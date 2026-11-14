"""Ownership and bounded reclaim for Astrid's disposable filesystem roots.

Only the explicitly registered roots below are inspected. Durable workspace
state (realm, CAS, drafts and production files) is deliberately outside this
registry.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_OWNED_ROOTS_FILE = "storage-owned-roots.json"
# Shared by the host's private incarnation directories and their reclaim policy.
ATTEMPT_ROOT_PREFIX = "astrid-attempt-"


@dataclass(frozen=True)
class RootPolicy:
    name: str
    purpose: str
    reader: str
    lifecycle: str
    path: Path
    ttl_seconds: int
    selector: Callable[[Path], bool]


def _tree_bytes(path: Path) -> int:
    total = 0
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
            for name in files:
                item = Path(root) / name
                try:
                    if not item.is_symlink():
                        total += item.stat().st_size
                except OSError:
                    pass
    return total


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def registered_roots(data_root: Path, *, temporary_root: Path | None = None, cache_root: Path | None = None) -> list[RootPolicy]:
    """Return only roots whose writer and consumer have an explicit policy."""
    data_root = Path(data_root).expanduser().absolute()
    temp = Path(temporary_root or tempfile.gettempdir())
    cache = Path(cache_root or (Path.home() / "Library" / "Caches" / "Astrid"))
    policies = [
        RootPolicy("media-view", "named convenience views of Runtime media", "SDK media handles", "expire after 30 days", data_root / "media-view", 30 * 86400, lambda p: p.is_file()),
        RootPolicy("astrid-open", "temporary media-open materializations", "media open caller", "expire after 1 day", data_root / "astrid-open", 86400, lambda p: p.is_file()),
        RootPolicy("astrid-open-temp", "fallback temporary media-open materializations", "media open caller", "expire after 1 day", temp / "astrid-open", 86400, lambda p: p.is_file()),
        RootPolicy("timeline-visualize", "timeline preview evidence cache", "timelines visualize", "existing LRU byte and directory cap; expire after 30 days", data_root / "timeline-visualize", 30 * 86400, lambda p: p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name) is not None),
        RootPolicy("fast-lane-objects", "fast timeline frame input cache", "timeline visualize fast lane", "size capped by fast-lane writer; expire after 14 days", temp / "astrid-fast-lane" / "objects", 14 * 86400, lambda p: p.is_file()),
        RootPolicy("fast-lane-capture", "per-call frame capture work", "timeline visualize fast lane", "expire after 1 day", temp / "astrid-fast-lane", 86400, lambda p: p.is_dir() and p.name.startswith("capture-")),
        RootPolicy("fast-lane-verify", "per-call timeline verification work", "timeline visualize fast lane", "expire after 7 days", temp / "astrid-fast-lane", 7 * 86400, lambda p: p.is_dir() and p.name.startswith("verify-")),
        RootPolicy("attempt-roots", "crash-abandoned executor attempts", "generic pack host", "expire after 2 days", temp, 2 * 86400, lambda p: p.is_dir() and p.name.startswith(ATTEMPT_ROOT_PREFIX)),
        RootPolicy("host-log-archives", "rotated host logs", "runtime operator diagnostics", "expire after 30 days", data_root, 30 * 86400, lambda p: p.is_file() and (p.name.endswith(".gz") or ".log." in p.name)),
        RootPolicy("runtime-presentations", "named copy-on-write render views", "runs open desktop media reader", "expire after 30 days", data_root / "runtime" / "realms", 30 * 86400, lambda p: p.is_file()),
        RootPolicy("replay-output", "renderer replay outputs", "renderer replay tooling", "expire after 7 days", data_root / "renderer", 7 * 86400, lambda p: p.is_dir() and p.name.endswith(".replay-output")),
        RootPolicy("replay-workdir", "renderer replay work directories", "renderer replay tooling", "expire after 2 days", data_root / "renderer", 2 * 86400, lambda p: p.is_dir() and p.name.endswith(".replay-workdir")),
        RootPolicy("legacy-cache", "unused pre data-root Astrid cache", "none (retired)", "remove after 30 days", cache, 30 * 86400, lambda p: p.is_dir() and p.name == "Astrid"),
    ]
    policies.extend(_registered_external_roots(data_root))
    return policies


def _registered_external_roots(data_root: Path) -> list[RootPolicy]:
    manifest = data_root / _OWNED_ROOTS_FILE
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    rows = payload.get("roots") if isinstance(payload, dict) else None
    policies = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("kind") not in {"replay-output", "replay-workdir"}:
            continue
        path = Path(str(row.get("path") or "")).expanduser()
        kind = str(row["kind"])
        # Never register production content or paths with symlinked ancestry.
        suffix = ".replay-output" if kind == "replay-output" else ".replay-workdir"
        if not path.is_absolute() or "production" in path.parts or not path.name.endswith(suffix):
            continue
        current = path
        if any(part.is_symlink() for part in (current, *current.parents)):
            continue
        ttl = 7 * 86400 if kind == "replay-output" else 2 * 86400
        policies.append(RootPolicy(kind, "renderer replay artifact", "renderer replay tooling", f"expire after {ttl // 86400} days", path, ttl, lambda p, suffix="." + kind: p.is_dir() and p.name.endswith(suffix)))
    return policies


def register_owned_root(data_root: Path, path: Path, kind: str) -> None:
    """Record a replay scratch root whose established location is beside its bundle."""
    data_root = Path(data_root).expanduser().absolute()
    path = Path(path).expanduser().absolute()
    if kind not in {"replay-output", "replay-workdir"} or "production" in path.parts:
        return
    expected_suffix = ".replay-output" if kind == "replay-output" else ".replay-workdir"
    if not path.name.endswith(expected_suffix) or path.is_symlink() or not path.is_dir():
        return
    manifest = data_root / _OWNED_ROOTS_FILE
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"version": 1, "roots": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {"version": 1, "roots": []}
    rows = payload.get("roots") if isinstance(payload, dict) else []
    rows = [row for row in rows if isinstance(row, dict) and row.get("path") != str(path)]
    rows.append({"kind": kind, "path": str(path)})
    data_root.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"version": 1, "roots": rows}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, manifest)


def _compact_owned_roots(data_root: Path) -> None:
    manifest = Path(data_root) / _OWNED_ROOTS_FILE
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    rows = payload.get("roots") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return
    kept = [row for row in rows if isinstance(row, dict) and Path(str(row.get("path") or "")).exists()]
    if len(kept) == len(rows):
        return
    temporary = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"version": 1, "roots": kept}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, manifest)


def _candidates(policy: RootPolicy) -> list[Path]:
    root = policy.path
    if not root.exists() or root.is_symlink():
        return []
    if policy.name in {"fast-lane-capture", "fast-lane-verify", "attempt-roots", "replay-output", "replay-workdir"}:
        if policy.name in {"replay-output", "replay-workdir"}:
            return [root] if policy.selector(root) and not root.is_symlink() else []
        try:
            prefixes = {
                "fast-lane-capture": ("capture-",),
                "fast-lane-verify": ("verify-",),
                "attempt-roots": (ATTEMPT_ROOT_PREFIX,),
            }[policy.name]
            candidates = root.iterdir() if not prefixes else (p for prefix in prefixes for p in root.glob(prefix + "*"))
            selected = [p for p in candidates if not p.is_symlink() and policy.selector(p)]
            if policy.name == "attempt-roots":
                # Host-incarnation directories contain ordinary attempt roots.
                # Age the attempt itself: progress in a child must not leave a
                # live spool subject to its older parent's expiry date. Flat
                # roots from older hosts and empty incarnation roots still age
                # under the same policy.
                attempts = []
                for parent in selected:
                    children = [p for p in parent.glob(ATTEMPT_ROOT_PREFIX + "*")
                                if not p.is_symlink() and policy.selector(p)]
                    attempts.extend(children or [parent])
                return attempts
            return selected
        except OSError:
            return []
    if policy.name == "legacy-cache":
        return [root] if policy.selector(root) else []
    if policy.name == "host-log-archives":
        try:
            return [p for p in root.iterdir() if not p.is_symlink() and policy.selector(p)]
        except OSError:
            return []
    if policy.name == "runtime-presentations":
        found = []
        try:
            for realm in root.iterdir():
                views = realm / "presentations" / "renders"
                if realm.is_symlink() or not realm.is_dir() or views.is_symlink() or not views.is_dir():
                    continue
                found.extend(p for p in views.iterdir() if not p.is_symlink() and policy.selector(p))
        except OSError:
            return []
        return found
    if policy.name == "timeline-visualize":
        found = []
        for project in root.iterdir():
            if project.is_symlink() or not project.is_dir():
                continue
            found.extend(p for p in project.iterdir() if not p.is_symlink() and policy.selector(p))
        return found
    return [p for p in root.rglob("*") if not p.is_symlink() and policy.selector(p)]


def _visualize_cache():
    """The guard-aware visualize LRU module, when this checkout has one."""
    try:
        from . import visualize_cache
    except ImportError:
        return None
    return visualize_cache


def reclaim(data_root: Path, *, dry_run: bool = False, now: float | None = None,
            temporary_root: Path | None = None, cache_root: Path | None = None) -> dict:
    """Report owned stale roots and optionally delete them, without following symlinks."""
    clock = time.time() if now is None else float(now)
    results = []
    lru_reclaimable = 0
    for policy in registered_roots(data_root, temporary_root=temporary_root, cache_root=cache_root):
        items = []
        for path in _candidates(policy):
            size = _tree_bytes(path)
            stale = _older_than(path, clock - policy.ttl_seconds)
            removed = False
            if stale and not dry_run:
                try:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                    removed = not path.exists()
                except OSError:
                    removed = False
            items.append({"path": str(path), "bytes": size, "age_seconds": max(0, int(clock - path.stat().st_mtime)) if path.exists() else None, "expired": stale, "removed": removed})
        results.append({"name": policy.name, "purpose": policy.purpose, "reader": policy.reader, "lifecycle": policy.lifecycle, "count": len(items), "bytes": sum(row["bytes"] for row in items), "expired_count": sum(row["expired"] for row in items), "reclaimed_bytes": sum(row["bytes"] for row in items if row["removed"]), "roots": items})
        if policy.name == "timeline-visualize" and _visualize_cache() is not None:
            # Keep the established guard-aware LRU policy as the enforcement
            # path for preview captures; calculate the same candidates here so
            # doctor and --dry-run report the cap reclaimable total.
            visualize_cache = _visualize_cache()
            captures = visualize_cache.list_capture_dirs(policy.path)
            cap_bytes = visualize_cache.max_cache_bytes()
            total_bytes, total_dirs = sum(item.bytes for item in captures), len(captures)
            expired = {Path(item["path"]).resolve() for item in items if item["expired"]}
            planned = 0
            planned_by_lru = 0
            for capture in captures:
                if total_bytes <= cap_bytes and total_dirs <= visualize_cache.DEFAULT_MAX_DIRS:
                    break
                guarded = clock - capture.mtime < visualize_cache.GUARD_SECONDS
                if capture.path.resolve() in expired:
                    planned += capture.bytes
                    total_bytes -= capture.bytes
                    total_dirs -= 1
                elif not guarded:
                    planned += capture.bytes
                    planned_by_lru += capture.bytes
                    total_bytes -= capture.bytes
                    total_dirs -= 1
            lru_reclaimable += planned_by_lru
            results[-1]["lru_reclaimable_bytes"] = planned_by_lru
            if not dry_run:
                report = visualize_cache.prune_visualize_cache(policy.path, max_bytes=cap_bytes, max_dirs=visualize_cache.DEFAULT_MAX_DIRS, now=clock)
                results[-1]["lru_reclaimed_bytes"] = report.freed_bytes
    expired_total = sum(item["bytes"] for row in results for item in row["roots"] if item["expired"])
    if not dry_run:
        _compact_owned_roots(data_root)
    return {"dry_run": dry_run, "roots": results, "bytes": sum(row["bytes"] for row in results), "count": sum(row["count"] for row in results), "reclaimable_bytes": expired_total + lru_reclaimable, "reclaimed_bytes": sum(row["reclaimed_bytes"] for row in results) + sum(row.get("lru_reclaimed_bytes", 0) for row in results)}


__all__ = ["RootPolicy", "reclaim", "register_owned_root", "registered_roots"]
