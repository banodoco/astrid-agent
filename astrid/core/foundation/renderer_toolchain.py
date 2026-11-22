"""One Node resolver for the renderer: the Remotion project's pin decides, the shell does not.

``remotion/package.json`` pins Node (``"engines": {"node": "=20.19.4"}``). The pack host
launch (``sdk/host_bootstrap``, so ``astrid dev promote`` too), ``doctor`` and the fast
lane all resolve the renderer's Node here:

1. ``ASTRID_NODE_EXECUTABLE`` is an explicit override. It must match the pin, or the
   launch is refused with the versions named. It never quietly falls back to something else.
2. Otherwise the first Node whose ``--version`` equals the pin, from: PATH, nvm, fnm,
   volta, asdf, mise, n, and official tarballs (Unix ``bin/node`` or Windows ``node.exe``)
   in a ``tools/`` folder beside the checkout or one of its parents.
3. If none matches, the result says what was found and where to put the pinned Node.
   A different Node on PATH is never used silently.

Without a pin (a project that declares no ``engines.node``), the first Node on PATH is used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

OVERRIDE_ENV = "ASTRID_NODE_EXECUTABLE"
_PROBE_TIMEOUT_S = 5
_NODE_PROBE_ENV = {
    "HOME", "PATH", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
}


@dataclass(frozen=True)
class NodeResolution:
    path: Path | None
    version: str | None
    pin: str | None  # "v20.19.4"
    source: str  # where it came from, in words
    problem: str | None = None  # why there is no usable Node (the refusal text)
    seen: tuple[str, ...] = field(default_factory=tuple)  # other Nodes looked at: "v26.7.0 at /opt/homebrew/bin/node"

    @property
    def ok(self) -> bool:
        return self.path is not None and self.problem is None


def node_pin(project_dir: Path) -> str | None:
    """``v20.19.4`` from ``engines.node`` (``=20.19.4``, ``20.19.4``, ``v20.19.4``); None if unpinned or a range."""
    try:
        raw = json.loads((Path(project_dir) / "package.json").read_text(encoding="utf-8"))["engines"]["node"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    match = re.fullmatch(r"\s*=?\s*v?(\d+\.\d+\.\d+)\s*", str(raw))
    return f"v{match.group(1)}" if match else None


def probe_version(node: Path | str) -> str | None:
    """``v20.19.4``, or None when it is not a working Node."""
    path = Path(node)
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        done = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
                              check=False, env={k: v for k, v in os.environ.items() if k.upper() in _NODE_PROBE_ENV})
    except (OSError, subprocess.SubprocessError):
        return None
    version = (done.stdout.strip().splitlines() or [""])[0]
    return version if done.returncode == 0 and re.fullmatch(r"v\d+\.\d+\.\d+.*", version) else None


def candidates(project_dir: Path, pin: str | None, *, search_path: str | None = None,
               home: Path | None = None) -> list[tuple[Path, str]]:
    """Where a Node may live, in order, with a word for each place."""
    home = home or Path.home()
    found: list[tuple[Path, str]] = []
    path_node = shutil.which("node", path=search_path)
    if path_node:
        found.append((Path(path_node), "PATH"))
    if pin:
        bare = pin.lstrip("v")
        found += [
            (home / ".nvm" / "versions" / "node" / pin / "bin" / "node", "nvm"),
            (home / ".nvm" / "versions" / "node" / pin / "node.exe", "nvm"),
            (home / ".local" / "share" / "fnm" / "node-versions" / pin / "installation" / "bin" / "node", "fnm"),
            (home / ".fnm" / "node-versions" / pin / "installation" / "node.exe", "fnm"),
            (home / "Library" / "Application Support" / "fnm" / "node-versions" / pin / "installation" / "bin" / "node", "fnm"),
            (home / ".volta" / "tools" / "image" / "node" / bare / "bin" / "node", "volta"),
            (home / ".volta" / "tools" / "image" / "node" / bare / "node.exe", "volta"),
            (home / ".asdf" / "installs" / "nodejs" / bare / "bin" / "node", "asdf"),
            (home / ".local" / "share" / "mise" / "installs" / "node" / bare / "bin" / "node", "mise"),
            (Path("/usr/local/n/versions/node") / bare / "bin" / "node", "n"),
            (home / ".local" / "share" / "mise" / "installs" / "node" / bare / "node.exe", "mise"),
        ]
        project = Path(project_dir).resolve(strict=False)
        for ancestor in [project, *list(project.parents)[:4]]:
            for root in (ancestor / "tools", ancestor):
                if root.is_dir():
                    for pattern in (f"node-{pin}-*/bin/node", f"node-{pin}-*/node.exe"):
                        for tarball in sorted(root.glob(pattern)):
                            found.append((tarball, f"tools beside the checkout ({root})"))
    unique: dict[str, tuple[Path, str]] = {}
    for path, where in found:
        unique.setdefault(str(path), (path, where))
    return list(unique.values())


def resolve_pinned_node(project_dir: Path, *, override: str | None = None, search_path: str | None = None,
                        home: Path | None = None) -> NodeResolution:
    """The renderer's Node for this Remotion project (see the module docstring)."""
    pin = node_pin(project_dir)
    package = Path(project_dir) / "package.json"
    if override:
        path = Path(override).expanduser()
        version = probe_version(path)
        if version is None:
            return NodeResolution(None, None, pin, f"{OVERRIDE_ENV}={override}",
                                  f"{OVERRIDE_ENV}={override} is not a working Node executable")
        if pin and version != pin:
            return NodeResolution(None, version, pin, f"{OVERRIDE_ENV}={override}",
                                  f"{OVERRIDE_ENV}={override} is Node {version}, but {package} pins {pin}: point it at "
                                  f"Node {pin}, or unset it and Astrid finds the pinned Node itself")
        return NodeResolution(path.resolve(), version, pin, f"{OVERRIDE_ENV} (explicit override)")
    seen: list[str] = []
    for path, where in candidates(project_dir, pin, search_path=search_path, home=home):
        version = probe_version(path)
        if version is None:
            continue
        if pin is None or version == pin:
            return NodeResolution(path.resolve(), version, pin, f"{'the first Node on ' if pin is None else ''}{where}",
                                  None, tuple(seen))
        seen.append(f"{version} at {path} ({where})")
    others = f"; found only {', '.join(seen[:4])}" if seen else "; no other Node either"
    return NodeResolution(None, None, pin, "nowhere",
                          f"no Node {pin} for the renderer ({package} pins it){others}. Install Node {pin} "
                          f"(nvm/fnm/volta, or unpack node-{pin}-<os>-<arch> into a tools/ folder beside the checkout), "
                          f"or set {OVERRIDE_ENV} to it", tuple(seen))


def resolve_remotion_project_dir(
    project_dir_env: str, *, repo_root: Path, environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an absolute configured project or the checkout's Remotion directory."""
    source = os.environ if environment is None else environment
    raw = source.get(project_dir_env, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{project_dir_env} must be an absolute path")
        return candidate.resolve()
    # Development source checkouts may use the conventional location.  A
    # wheel install has no sibling remotion/ tree, so this correctly fails
    # closed until deployment supplies the trusted runtime configuration.
    return (repo_root / "remotion").resolve()
