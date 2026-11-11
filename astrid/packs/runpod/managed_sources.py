"""Agent-managed bootstrap source root for RunPod preparation.

The Agent derives the pinned checkouts itself under its data root; no caller
sets a source-root variable.  Each directory is a clean detached checkout of
the exact pinned commit, fetched by SHA.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

LOGGER = logging.getLogger(__name__)
AGENT_ROOT = Path(__file__).resolve().parents[3]
_GIT_ENV_DROP = "GIT_"


class ManagedSourcesError(RuntimeError):
    """The managed bootstrap sources cannot be materialized."""


def managed_source_root() -> Path:
    from astrid.sdk.storage_root import resolve_runtime_data_root

    return resolve_runtime_data_root() / "runpod" / "sources"


def _installed_manifest_path() -> Path:
    """The release manifest the installer wrote at this install's version root.

    `stage()` writes ``<version>/manifest.json`` and the Agent runs from
    ``<version>/py``, so the version root is the interpreter prefix's parent
    (the same layout `mcp_server._astrid_env` and `pack.cli_manage` rely on).
    """
    return Path(sys.prefix).absolute().parent / "manifest.json"


def _pins() -> dict[str, tuple[str, str]]:
    """Map each remote_placement.SOURCE_DIRS name to (repository, commit)."""
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.canonical_pins import canonical_pin
    from astrid.core.rendering._local_schema import runtime_lock_path

    runtime_commit = json.loads(runtime_lock_path(AGENT_ROOT).read_bytes())["source_commit"]
    vibe = next(s for s in engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130").sources
                if s.name == "vibecomfy").pin
    manifest_path = _installed_manifest_path()
    try:
        manifest = json.loads(manifest_path.read_bytes())
        agent, runtime, lifecycle = manifest["agent"], manifest["runtime"], manifest["lifecycle"]
    except (OSError, ValueError, KeyError) as exc:
        raise ManagedSourcesError(
            f"RunPod needs the installed release manifest at {manifest_path} to pin its bootstrap sources") from exc
    if runtime["sha"] != runtime_commit:
        raise ManagedSourcesError("installed manifest runtime pin differs from runtime.lock.json")
    # The lifecycle and VibeComfy pods run are the Agent's canonical pins; a manifest that names
    # a different lifecycle or VibeComfy would split what is installed locally from what ships.
    canonical = canonical_pin("lifecycle")
    if (lifecycle["sha"], lifecycle["repo"]) != (canonical["revision"], canonical["repository"]):
        raise ManagedSourcesError("installed manifest lifecycle pin differs from the Agent's canonical pin")
    if manifest.get("vibecomfy", {}).get("sha", vibe.revision) != vibe.revision:
        raise ManagedSourcesError("installed manifest VibeComfy pin differs from the Agent's canonical pin")
    return {
        "src/astrid-agent": (agent.get("repo", "https://github.com/banodoco/astrid-agent"), agent["sha"]),
        "src/astrid-runtime": (runtime.get("repo", "https://github.com/banodoco/astrid-runtime"), runtime_commit),
        "src/runpod-lifecycle": (canonical["repository"], canonical["revision"]),
        "src/VibeComfy": (vibe.repository, vibe.revision),
    }


def _git(directory: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_GIT_ENV_DROP)}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(directory), *args], env=env, capture_output=True,
                            text=True, timeout=1800, check=False)
    if result.returncode:
        raise ManagedSourcesError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _current(path: Path, sha: str) -> bool:
    try:
        return (path / ".git").is_dir() and _git(path, "rev-parse", "HEAD") == sha \
            and not _git(path, "status", "--porcelain", "--untracked-files=all")
    except (ManagedSourcesError, OSError, subprocess.SubprocessError):
        return False


def _checkout(path: Path, repository: str, sha: str) -> None:
    staging = path.with_name(path.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _git(staging, "init", "--quiet")
        _git(staging, "fetch", "--quiet", "--depth=1", repository, sha)
        _git(staging, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        if _git(staging, "rev-parse", "HEAD") != sha:
            raise ManagedSourcesError(f"fetched checkout of {repository} is not {sha}")
        shutil.rmtree(path, ignore_errors=True)
        os.replace(staging, path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_managed_sources() -> Path:
    """Return the managed source root with every pinned checkout present and clean."""
    root = managed_source_root()
    for relative, (repository, sha) in _pins().items():
        path = root / relative
        if _current(path, sha):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("runpod managed source %s: fetching %s at %s", relative, repository, sha)
        _checkout(path, repository, sha)
    return root
