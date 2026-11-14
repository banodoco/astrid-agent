#!/usr/bin/env python3
"""Read-only inventory of model files and scanned references on a volume."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

MAX_METADATA_BYTES = 2 * 1024 * 1024
NAMES = {"models.lock.json", "model-inventory.json", "release-manifest.json"}


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def no_symlinks(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(item.lstat().st_mode):
                raise ValueError(f"path traverses symlink: {item}")
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict:
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
            raise ValueError(f"unsafe or oversized metadata: {path}")
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError(f"oversized metadata: {path}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"metadata is not an object: {path}")
    return value


def relative(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid model path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe model path")
    return path.as_posix()


def absolute_root(value) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("model root must be absolute")
    path = Path(value)
    if ".." in path.parts or os.path.normpath(value) != value:
        raise ValueError("model root must be normalized")
    return path


def walk(root: Path, device: int, symlinks=False):
    try:
        if stat.S_ISLNK(root.lstat().st_mode):
            raise ValueError(f"root is a symlink: {root}")
    except FileNotFoundError:
        return
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info, path = entry.stat(follow_symlinks=False), Path(entry.path)
                if info.st_dev != device:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) or (symlinks and stat.S_ISLNK(info.st_mode)):
                    yield path, info


def metadata(mount: Path, device: int):
    for path, info in walk(mount, device, symlinks=True):
        if path.name in NAMES or (path.name == "install.json" and path.parent.name == ".astrid-engine"):
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"recognized metadata is not regular: {path}")
            yield path


def rows(doc: dict, source: Path, root: Path, absolute=False, alternate_roots=()) -> list[dict]:
    values = doc.get("models")
    if not isinstance(values, list):
        raise ValueError(f"metadata lacks models array: {source}")
    result = []
    for row in values:
        if not isinstance(row, dict):
            raise ValueError(f"invalid model row: {source}")
        model_root = root
        name = row.get("destination") or row.get("path")
        if not name and row.get("subdir") and row.get("name"):
            name = f"{row['subdir']}/{row['name']}"
        if absolute and isinstance(name, str) and name.startswith("/"):
            path = Path(name)
            match = next((candidate for candidate in (root, *alternate_roots) if within(path, candidate)), None)
            if match is None:
                raise ValueError(f"engine model path escapes root: {source}")
            model_root = match
            name = path.relative_to(model_root).as_posix()
        result.append({"path": relative(name), "root": str(model_root), "bytes": row.get("bytes", row.get("size")),
                       "sha256": row.get("sha256")})
    return result


def audit(mount: Path, models_root: Path, engine_roots=(), locks=()) -> dict:
    mount = Path(os.path.abspath(mount))
    if not stat.S_ISDIR(mount.lstat().st_mode):
        raise ValueError("mount must be a real directory")
    no_symlinks(mount)
    device = mount.lstat().st_dev
    models_root = Path(os.path.abspath(models_root))
    if not within(models_root, mount):
        raise ValueError("models root must be under mount")
    no_symlinks(models_root)
    sources: list[dict[str, Any]] = []
    roots, seen = {str(models_root)}, set()
    pending = [mount]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info, path = entry.stat(follow_symlinks=False), Path(entry.path)
                if info.st_dev == device and stat.S_ISDIR(info.st_mode):
                    if entry.name == "models":
                        roots.add(str(path))
                    pending.append(path)

    def add(source: Path, kind: str, forced_root=None):
        if source in seen:
            return
        seen.add(source)
        doc = read_json(source)
        alternate_roots: tuple[Path, ...] = ()
        if kind == "engine_receipt":
            value = doc.get("models_root")
            root = absolute_root(value) if isinstance(value, str) and value else source.parent.parent / "runtime/ComfyUI/models"
            if not value:
                alternate_roots = (source.parent.parent / "models",)
        elif forced_root is not None:
            root = forced_root
        elif isinstance(doc.get("model_root"), str):
            root = absolute_root(doc["model_root"])
        else:
            root = source.parent / "models"
        if not root.is_absolute() or ".." in root.parts or os.path.normpath(str(root)) != str(root):
            raise ValueError(f"model root must be absolute and normalized: {source}")
        inside = within(root, mount)
        if kind != "engine_receipt" and not inside:
            raise ValueError(f"model root escapes mount: {source}")
        scan_roots = (root, *alternate_roots)
        if inside:
            no_symlinks(root)
        for candidate in scan_roots:
            if within(candidate, mount):
                no_symlinks(candidate)
                roots.add(str(candidate))
        found = rows(doc, source, root, absolute=kind == "engine_receipt", alternate_roots=alternate_roots)
        sources.append({"source": str(source), "kind": kind, "root": str(root), "root_scanned": inside,
                        "models": [{**row, "file": str(Path(row["root"]) / row["path"])} for row in found]})

    for source in metadata(mount, device):
        add(source, "engine_receipt" if source.name == "install.json" else source.name)
    for root in engine_roots:
        add(Path(root) / ".astrid-engine/install.json", "engine_receipt")
    for lock in locks:
        add(Path(lock), "supplied_lock", models_root)

    files: list[dict[str, Any]] = []
    inodes: dict[tuple[int, int], str] = {}
    seen_paths: set[str] = set()
    for root_text in sorted(roots):
        for path, info in walk(Path(root_text), device):
            if str(path) in seen_paths:
                continue
            seen_paths.add(str(path))
            inode = (info.st_dev, info.st_ino)
            duplicate = inode in inodes
            files.append({"path": str(path), "root": root_text, "bytes": info.st_size,
                          "accounted_bytes": 0 if duplicate else info.st_size,
                          "hardlink_of": inodes.get(inode) if duplicate else None})
            inodes.setdefault(inode, str(path))

    for source in sources:
        for model in source["models"]:
            path = Path(model["file"])
            try:
                no_symlinks(path)
                info = path.lstat()
            except FileNotFoundError:
                model["status"] = "missing"
            except ValueError:
                model["status"] = "symlink_or_other_device_or_type"
            else:
                model["status"] = "regular" if stat.S_ISREG(info.st_mode) and info.st_dev == device else "symlink_or_other_device_or_type"
                model["actual_bytes"] = info.st_size if stat.S_ISREG(info.st_mode) else None
        source["model_count"] = len(source["models"])
        source["declared_bytes"] = sum(m["bytes"] for m in source["models"] if type(m.get("bytes")) is int and m["bytes"] >= 0)
        source["present_bytes"] = sum(m.get("actual_bytes", 0) or 0 for m in source["models"] if m.get("status") == "regular")

    scanned = {item["path"] for item in files}
    referenced = {m["file"] for s in sources for m in s["models"]}
    candidates = roots
    unclassified = sorted(p for p in scanned if p not in referenced and any(within(Path(p), Path(r)) for r in candidates))
    return {"schema": "astrid.model-storage-audit.v1", "mount": str(mount), "device": device,
            "models_roots": sorted(roots), "sources": sources, "files": sorted(files, key=lambda f: f["path"]),
            "scanned_file_bytes": sum(f["accounted_bytes"] for f in files),
            "not_in_scanned_references": unclassified, "deletable": [], "reference_coverage_complete": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", default="/workspace")
    parser.add_argument("--models-root", default="/workspace/astrid/engines/comfyui/models")
    parser.add_argument("--engine-root", action="append", default=[])
    parser.add_argument("--lock", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="Required acknowledgement; this command only reads")
    args = parser.parse_args(argv)
    try:
        result = audit(Path(args.mount), Path(args.models_root), args.engine_root, args.lock)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "astrid.model-storage-audit.v1", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
