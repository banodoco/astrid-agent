"""Declaration and collection of run log files.

This module deliberately uses only the standard library so its source can be
sent to a remote Python process without installing Astrid.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "run-logs.v1"
DECLARATION_NAME = "logs.json"
LOGS_DIR = "logs"
INDEX_NAME = "index.json"
LOGS_MANIFEST_MARK = "LOGS_MANIFEST_SHA256="
CAP_BYTES = 256 * 1024 * 1024
ROLES = ("engine", "run", "stdout", "stderr", "install")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}\Z")
_TIMESTAMP = re.compile(r"^\s*(\d{4}-\d\d-\d\d[T ][^\s]+|\d{10}(?:\.\d+)?)")
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S,%f")


@dataclasses.dataclass(frozen=True)
class LogDecl:
    name: str
    role: str
    path: str
    start: int = 0
    end: int | None = None
    since: str | None = None
    until: str | None = None
    line_time: str = "iso8601"


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _load(root: Path) -> dict[str, Any]:
    path = root / DECLARATION_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "logs": [], "children": []}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("invalid run log declaration")
    logs = value.get("logs", [])
    children = value.get("children", [])
    if not isinstance(logs, list) or not isinstance(children, list):
        raise ValueError("logs and children must be lists")
    return {"schema": SCHEMA, "logs": logs, "children": children}


def declare(root: Path, decl: LogDecl) -> None:
    root = Path(root)
    if not _NAME.fullmatch(decl.name) or decl.role not in ROLES:
        raise ValueError("invalid log name or role")
    if not isinstance(decl.path, str) or not decl.path:
        raise ValueError("path must be a non-empty string")
    if not isinstance(decl.start, int) or isinstance(decl.start, bool) or decl.start < 0:
        raise ValueError("start must be a non-negative integer")
    if decl.end is not None and (not isinstance(decl.end, int) or decl.end < 0):
        raise ValueError("end must be a non-negative integer or null")
    if decl.line_time not in ("iso8601", "epoch", *_FORMATS):
        raise ValueError("unsupported line_time")
    _time_bounds(decl)
    root.mkdir(parents=True, exist_ok=True)
    data = _load(root)
    item = dataclasses.asdict(decl)
    data["logs"] = [row for row in data["logs"] if not isinstance(row, dict) or row.get("name") != decl.name]
    data["logs"].append(item)
    _atomic_json(root / DECLARATION_NAME, data)


def declare_child(root: Path, child: Path) -> None:
    root = Path(root).resolve()
    child = Path(child)
    resolved = child.resolve() if child.is_absolute() else (root / child).resolve()
    if not _inside(root, resolved):
        raise ValueError("child declaration must be inside root")
    root.mkdir(parents=True, exist_ok=True)
    data = _load(root)
    rel = os.path.relpath(resolved, root)
    if rel not in data["children"]:
        data["children"].append(rel)
    _atomic_json(root / DECLARATION_NAME, data)


def read_declared(root: Path) -> list[tuple[LogDecl, Path]]:
    root = Path(root).resolve()
    results: list[tuple[LogDecl, Path]] = []
    visited: set[Path] = set()

    def walk(directory: Path, depth: int) -> None:
        directory = directory.resolve()
        if directory in visited or depth > 4:
            return
        visited.add(directory)
        data = _load(directory)
        for item in data["logs"]:
            decl = LogDecl(**{k: item[k] for k in LogDecl.__dataclass_fields__ if k in item})
            source = Path(decl.path)
            results.append((decl, source if source.is_absolute() else directory / source))
        for child in data["children"]:
            child_path = Path(child)
            path = child_path if child_path.is_absolute() else directory / child_path
            if _inside(root, path.resolve()):
                walk(path.parent if path.name == DECLARATION_NAME else path, depth + 1)

    walk(root, 0)
    return results


def _parse_time(value: str, fmt: str) -> float | None:
    if fmt == "epoch":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in _FORMATS:
            try:
                parsed = _dt.datetime.strptime(value, pattern).replace(tzinfo=_dt.timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _time_bounds(decl: LogDecl) -> tuple[float, float]:
    bounds = []
    for name, value, default in (("since", decl.since, float("-inf")),
                                 ("until", decl.until, float("inf"))):
        if value is None:
            bounds.append(default)
            continue
        parsed = _parse_time(value, "iso8601") if isinstance(value, str) else None
        if parsed is None:
            raise ValueError(f"{name} must be a valid ISO time: {value!r}")
        bounds.append(parsed)
    return bounds[0], bounds[1]


def _time_slice(payload: bytes, decl: LogDecl) -> bytes:
    if decl.since is None and decl.until is None:
        return payload
    lower, upper = _time_bounds(decl)
    lines = payload.splitlines(keepends=True)
    kept: list[bytes] = []
    current: float | None = None
    for line in lines:
        match = _TIMESTAMP.match(line.decode("utf-8", "replace"))
        if match:
            current = _parse_time(match.group(1), decl.line_time)
        if current is not None and lower <= current <= upper:
            kept.append(line)
    return b"".join(kept)


def collect(root: Path, *, cap_bytes: int = CAP_BYTES, extra_root: Path | None = None) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    merging = extra_root is not None and (root / LOGS_DIR / INDEX_NAME).exists()
    rows: list[dict[str, Any]] = read_index(root) if merging else []
    brought_names = {row.get("name"): row.get("source", "transported index") for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}
    seen_names: dict[str, int] = {}
    try:
        root.mkdir(parents=True, exist_ok=True)
        visited: set[Path] = set()
        count = 0

        def overflow() -> None:
            if not any(row.get("name") == "overflow" for row in rows):
                rows.append({"name": "overflow", "missing": True, "reason": "limit"})

        def walk(directory: Path, depth: int) -> None:
            nonlocal count
            directory = directory.resolve()
            if directory in visited:
                return
            visited.add(directory)
            try:
                data = _load(directory)
            except Exception as exc:  # noqa: BLE001 - collection must report every source failure
                rows.append({"name": "declaration", "missing": True, "reason": type(exc).__name__})
                return
            for item in data["logs"]:
                if count >= 32:
                    overflow()
                    return
                count += 1
                try:
                    decl = LogDecl(**{k: item[k] for k in LogDecl.__dataclass_fields__ if k in item})
                    if not _NAME.fullmatch(decl.name) or decl.role not in ROLES:
                        raise ValueError("invalid declaration")
                    source = Path(decl.path)
                    source = source if source.is_absolute() else directory / source
                    if decl.name in brought_names:
                        rows.append({"name": decl.name, "missing": True, "reason": "duplicate_name",
                                     "source": str(source), "first_source": str(brought_names[decl.name])})
                        continue
                    brought_names[decl.name] = str(source)
                    if source.is_symlink():
                        raise OSError("symlink source")
                    stat = source.stat()
                    if not source.is_file():
                        raise FileNotFoundError(str(source))
                    start = decl.start if decl.start <= stat.st_size else 0
                    end = min(decl.end, stat.st_size) if decl.end is not None else stat.st_size
                    if end < start:
                        end = start
                    with source.open("rb") as stream:
                        stream.seek(start)
                        payload = stream.read(end - start)
                    payload = _time_slice(payload, decl)
                    truncated = len(payload) > cap_bytes
                    gap = b"\n...[run-logs gap]...\n"
                    if truncated:
                        keep = max(0, cap_bytes - len(gap))
                        head = keep // 2
                        tail = keep - head
                        payload = payload[:head] + gap + (payload[-tail:] if tail else b"")
                    ordinal = seen_names.get(decl.name, 0) + 1
                    seen_names[decl.name] = ordinal
                    name = decl.name if ordinal == 1 else f"{decl.name}-{ordinal}"
                    suffix = source.suffix if source.suffix in (".log", ".json", ".txt") else ".log"
                    filename = name + suffix
                    destination = root / LOGS_DIR / filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    rows.append({"name": name, "role": decl.role, "file": filename, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "truncated": truncated, "missing": False, "source": str(source)})
                except Exception as exc:  # noqa: BLE001 - collection must not raise
                    name = item.get("name", "unknown") if isinstance(item, dict) else "unknown"
                    ordinal = seen_names.get(name, 0) + 1
                    seen_names[name] = ordinal
                    rows.append({"name": name if ordinal == 1 else f"{name}-{ordinal}", "missing": True, "reason": type(exc).__name__})
            for child in data["children"]:
                if depth >= 4 or count >= 32:
                    overflow()
                    return
                path = Path(child)
                child_decl = path if path.is_absolute() else directory / path
                if not _inside(root, child_decl.resolve()):
                    rows.append({"name": "child", "missing": True, "reason": "ValueError"})
                    continue
                walk(child_decl.parent if child_decl.name == DECLARATION_NAME else child_decl, depth + 1)

        if not merging:
            walk(root, 0)
        if extra_root is not None:
            walk(Path(extra_root), 0)
    except Exception as exc:  # noqa: BLE001 - collection must not raise
        rows.append({"name": "collection", "missing": True, "reason": type(exc).__name__})
    try:
        _atomic_json(root / LOGS_DIR / INDEX_NAME, {"schema": "run-logs.collected.v1", "logs": rows})
    except Exception:  # noqa: BLE001 - index writing must not escape collect
        pass
    return rows


def read_index(root: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads((Path(root) / LOGS_DIR / INDEX_NAME).read_text(encoding="utf-8"))
        return value.get("logs", []) if isinstance(value, dict) and isinstance(value.get("logs"), list) else []
    except (OSError, ValueError, TypeError):
        return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run_logs")
    subs = parser.add_subparsers(dest="command", required=True)
    p_declare = subs.add_parser("declare")
    p_declare.add_argument("root")
    p_declare.add_argument("--name", required=True)
    p_declare.add_argument("--role", required=True, choices=ROLES)
    p_declare.add_argument("--path", required=True)
    p_declare.add_argument("--start", type=int, default=0)
    p_collect = subs.add_parser("collect")
    p_collect.add_argument("root")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "declare":
        try:
            declare(Path(args.root), LogDecl(args.name, args.role, args.path, args.start))
        except (OSError, ValueError):
            return 2
        return 0
    collect(Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
