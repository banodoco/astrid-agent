"""Agent-managed bootstrap source root for RunPod preparation.

The Agent derives the pinned checkouts itself under its data root; no caller
sets a source-root variable.  Each directory is a clean detached checkout of
the exact pinned commit, fetched by SHA.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

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


_REMEDY = ("Install a release manifest whose pins match this Agent, or change "
           "astrid/packs/runpod/pins.json in this checkout to the pin you want to test.")


def _show_repository(value: object) -> str:
    identity = _repository_identity(value)
    return f"https://{identity[0]}{identity[1]}" if identity is not None else "<invalid>"


def _show_sha(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else "<invalid>"


def _show_version(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(
        r"[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:\.dev[0-9]+)?", value) else "<invalid>"


def _repository_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    if (len(value) > 200 or not value.startswith("https://")
            or any(char.isspace() or unicodedata.category(char) == "Cc" for char in value)):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.netloc or "@" in url.netloc or ":" in url.netloc
                or "[" in url.netloc or "]" in url.netloc or "?" in value or "#" in value
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", url.netloc, re.IGNORECASE)
                or not url.path.startswith("/") or not url.path):
            return None
        labels = url.netloc.lower().split(".")
        if ((len(labels) == 4 and all(label.isdigit() for label in labels))
                or any(len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
               for label in labels)):
            return None
        segments = url.path[1:].split("/")
        if segments and segments[-1] == "":
            segments.pop()
        if (not segments or any(not segment or segment in {".", ".."} or "%" in segment
                                or not re.fullmatch(r"[A-Za-z0-9._-]+", segment) for segment in segments)):
            return None
    except ValueError:
        return None
    path = "/" + "/".join(segments)
    return url.netloc.lower(), path


def pin_mismatch_message(name: str, manifest_pin: dict, canonical: dict) -> str:
    version = (f" version {_show_version(manifest_pin.get('version'))}." if "version" in manifest_pin else "")
    canonical_version = (f" version {_show_version(canonical.get('version'))}." if "version" in canonical else "")
    return (f"installed manifest {name} pin differs from the Agent's canonical pin. "
            f"Manifest: repository {_show_repository(manifest_pin.get('repository'))} revision {_show_sha(manifest_pin.get('revision'))}.{version} "
            f"Agent (astrid/packs/runpod/pins.json): repository {_show_repository(canonical.get('repository'))} "
            f"revision {_show_sha(canonical.get('revision'))}.{canonical_version} {_REMEDY}")


SOURCE_MODE_ENV = "ASTRID_RUNPOD_SOURCE_MODE"
SOURCE_MODES = ("installed", "checkout")
_SHA = re.compile(r"[0-9a-f]{40}")
_RUNTIME_REPOSITORY = "https://github.com/banodoco/astrid-runtime"


def source_mode() -> str:
    """``installed`` (default) or ``checkout``; chosen only by ASTRID_RUNPOD_SOURCE_MODE or `--sources`."""
    value = os.environ.get(SOURCE_MODE_ENV, "").strip().lower() or "installed"
    if value not in SOURCE_MODES:
        raise ManagedSourcesError(
            f"{SOURCE_MODE_ENV}={value!r} is not a RunPod source mode; use one of: {', '.join(SOURCE_MODES)}")
    return value


def select_source_mode(value: str | None) -> None:
    """Apply an explicit `--sources` choice for this process (the visible form of the env var)."""
    if value:
        os.environ[SOURCE_MODE_ENV] = value


def _refusal(problem: str, fix: str) -> ManagedSourcesError:
    return ManagedSourcesError(f"RunPod source mode 'checkout' refused: {problem}. {fix}")


def _scp_to_https(url: str) -> str:
    """git@host:o/r.git and ssh://git@host/o/r.git name the same repository the worker fetches over https."""
    match = re.fullmatch(r"(?:ssh://)?git@([A-Za-z0-9.-]+)[:/](.+)", url)
    return f"https://{match.group(1)}/{match.group(2)}" if match else url


def _checkout_remote(root: Path) -> tuple[str, str]:
    name = "origin"
    try:
        branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        configured = _git(root, "config", "--get", f"branch.{branch}.remote")
        if configured and configured != ".":
            name = configured
    except ManagedSourcesError:
        pass  # detached HEAD or no upstream: origin
    try:
        url = _git(root, "config", "--get", f"remote.{name}.url")
    except ManagedSourcesError:
        url = ""
    if not url:
        raise _refusal(f"the checkout has no remote named {name!r}, so the worker has nowhere to fetch it from",
                       f"Add one with `git remote add {name} https://github.com/<owner>/astrid-agent.git`, "
                       "push, and retry")
    return name, _scp_to_https(url)


def _head_is_on_remote(root: Path, head: str, name: str, url: str) -> bool:
    try:
        listing = _git(root, "ls-remote", url)
    except ManagedSourcesError as exc:
        raise _refusal(f"cannot reach remote {name!r} ({url}) to confirm HEAD {head[:12]} is pushed ({exc})",
                       "Check the network and the remote URL, then retry") from exc
    tips = [line.split("\t")[0] for line in listing.splitlines() if "\t" in line]
    if head in tips:
        return True
    for tip in dict.fromkeys(tips):
        try:
            _git(root, "cat-file", "-e", f"{tip}^{{commit}}")
            _git(root, "merge-base", "--is-ancestor", head, tip)
            return True
        except ManagedSourcesError:
            continue  # tip not present locally, or HEAD is not behind it
    return False


def derive_checkout_manifest(root: Path | None = None) -> tuple[dict, dict[str, str]]:
    """Derive, in memory, the release manifest an installer would write for this source checkout.

    Returns ``(manifest, provenance)``. The manifest has the installed schema and goes through the
    same validation in ``_pins``; provenance says where each pin came from. Refuses, before any claim
    or spend, whenever the rented machine could not fetch exactly the code this checkout runs.
    """
    from astrid.core.generation.canonical_pins import CANONICAL_PINS_PATH, canonical_pin
    root = Path(root) if root is not None else AGENT_ROOT
    if not (root / ".git").exists():
        raise _refusal(f"{root} is not a git checkout",
                       f"Run from a source checkout, or unset {SOURCE_MODE_ENV} to use the installed release manifest")
    dirty = _git(root, "diff-index", "--name-only", "HEAD", "--")  # tracked files, staged or not
    if dirty:
        files = dirty.splitlines()
        shown = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
        raise _refusal(f"tracked files have uncommitted changes ({shown}); the worker fetches commits, "
                       "not your working tree", "Commit and push them (or `git stash`), then retry")
    head = _git(root, "rev-parse", "HEAD")
    if not _SHA.fullmatch(head):
        raise _refusal("HEAD is not a commit", "Check out a branch with at least one commit")
    name, url = _checkout_remote(root)
    if not _head_is_on_remote(root, head, name, url):
        raise _refusal(f"HEAD {head[:12]} is not on remote {name!r} ({url}); the rented machine could not fetch it",
                       f"Run `git push {name} HEAD` (or push your branch), then retry")
    lock_path = root / "runtime.lock.json"  # the checkout's own pin file, never a packaged fallback
    try:
        runtime_sha = json.loads(Path(lock_path).read_bytes())["source_commit"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _refusal(f"the Runtime pin cannot be read from {lock_path} (source_commit)",
                       "Restore runtime.lock.json from the commit you are testing") from exc
    if not isinstance(runtime_sha, str) or not _SHA.fullmatch(runtime_sha):
        raise _refusal(f"{lock_path} source_commit is not a 40-hex commit", "Restore runtime.lock.json")
    pins = {}
    for source in ("lifecycle", "vibecomfy"):
        try:
            pin = canonical_pin(source)
            if not _SHA.fullmatch(str(pin["revision"])) or not pin["repository"]:
                raise ValueError(source)
        except Exception as exc:  # noqa: BLE001 - any unreadable or malformed pin is the same refusal
            raise _refusal(f"the {source} pin cannot be determined from {CANONICAL_PINS_PATH}",
                           "Restore that file from the commit you are testing") from exc
        pins[source] = pin
    lifecycle, vibecomfy = pins["lifecycle"], pins["vibecomfy"]
    manifest = {
        "agent": {"repo": url, "sha": head},
        "runtime": {"repo": _RUNTIME_REPOSITORY, "sha": runtime_sha},
        "lifecycle": {"repo": lifecycle["repository"], "sha": lifecycle["revision"],
                      **({"version": lifecycle["version"]} if "version" in lifecycle else {})},
        "vibecomfy": {"repo": vibecomfy["repository"], "sha": vibecomfy["revision"]},
    }
    provenance = {
        "agent": f"HEAD of the checkout at {root}, confirmed on remote {name!r}",
        "runtime": f"source_commit in {lock_path}",
        "lifecycle": CANONICAL_PINS_PATH,
        "vibecomfy": CANONICAL_PINS_PATH,
    }
    return manifest, provenance


def checkout_source_report() -> dict:
    """What a worker would fetch in checkout mode, and where each pin came from (for dry runs)."""
    manifest, provenance = derive_checkout_manifest()
    pins = {}
    for name in ("agent", "runtime", "lifecycle", "vibecomfy"):
        entry = manifest[name]
        pins[name] = {"repository": entry["repo"], "revision": entry["sha"], "from": provenance[name],
                      **({"version": entry["version"]} if "version" in entry else {})}
    return {"mode": "checkout", "pins": pins}


def preflight_source_mode() -> dict | None:
    """Refuse before any claim or spend when checkout mode is selected but its pins cannot be served.

    Returns the pin report in checkout mode, ``None`` in installed mode (which is unchanged).
    """
    if source_mode() != "checkout":
        return None
    report = checkout_source_report()
    _pins()  # the same validation the preparation will run
    return report


def _pins() -> dict[str, tuple[str, str]]:
    """Map each remote_placement.SOURCE_DIRS name to (repository, commit)."""
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.canonical_pins import canonical_pin
    from astrid.core.rendering._local_schema import runtime_lock_path

    runtime_commit = json.loads(runtime_lock_path(AGENT_ROOT).read_bytes())["source_commit"]
    vibe = next(s for s in engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130").sources
                if s.name == "vibecomfy").pin
    if source_mode() == "checkout":
        manifest, _ = derive_checkout_manifest()
        manifest_path = None
        label = "checkout-derived release manifest"
    else:
        manifest_path = _installed_manifest_path()
        label = f"installed release manifest at {manifest_path}"
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ManagedSourcesError(
                f"RunPod needs the installed release manifest at {manifest_path} to pin its bootstrap sources") from exc
    if not isinstance(manifest, dict):
        raise ManagedSourcesError(f"the {label} is not a JSON object")
    try:
        agent, runtime = manifest["agent"], manifest["runtime"]
        runtime_sha = runtime["sha"]
    except (KeyError, TypeError) as exc:
        raise ManagedSourcesError(
            f"RunPod needs the {label} to name the agent and runtime pins") from exc
    lifecycle = manifest.get("lifecycle")
    if lifecycle is None:
        raise ManagedSourcesError(
            f"the {label} has no lifecycle pin; RunPod needs one that matches "
            f"this Agent. {_REMEDY}")
    if not isinstance(lifecycle, dict):
        raise ManagedSourcesError(
            f"the {'installed release manifest' if manifest_path else label}'s lifecycle pin is not an object. {_REMEDY}")
    if runtime_sha != runtime_commit:
        raise ManagedSourcesError(
            f"{'installed manifest' if manifest_path else label} runtime pin differs from runtime.lock.json")
    # The lifecycle and VibeComfy pods run are the Agent's canonical pins; a manifest that names
    # a different lifecycle or VibeComfy would split what is installed locally from what ships.
    canonical = canonical_pin("lifecycle")
    if (_repository_identity(lifecycle.get("repo")) != _repository_identity(canonical["repository"])
            or lifecycle.get("sha") != canonical["revision"]
            or lifecycle.get("version") != canonical.get("version")):
        raise ManagedSourcesError(pin_mismatch_message(
            "lifecycle", {"repository": lifecycle.get("repo"), "revision": lifecycle.get("sha"),
                          "version": lifecycle.get("version")}, canonical))
    manifest_vibe = manifest.get("vibecomfy", {})
    if not isinstance(manifest_vibe, dict):
        raise ManagedSourcesError(
            f"the installed release manifest's vibecomfy pin is not an object. {_REMEDY}")
    if manifest_vibe.get("sha", vibe.revision) != vibe.revision:
        raise ManagedSourcesError(pin_mismatch_message(
            "VibeComfy", {"repository": manifest_vibe.get("repo"), "revision": manifest_vibe.get("sha")},
            {"repository": vibe.repository, "revision": vibe.revision}))
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
