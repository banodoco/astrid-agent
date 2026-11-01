"""Install-time browser pack builder. The installer owns staging and admission.

The caller supplies the release lock; this module never downloads tools, executes
pack code, installs a pack, or refreshes inventory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import unicodedata
from typing import Any, Mapping, TypedDict

from .pack_builder import BUILDER_SOURCE


class BuildRequest(TypedDict):
    version: int
    transaction_id: str
    pack_id: str
    stage_root: str
    source_digest: str
    entry: str | None
    extension_manifest: dict[str, Any] | None
    toolchain: dict[str, Any]


BuildResult = dict[str, Any]
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
_SINGLETONS = frozenset({"react", "react/jsx-runtime", "react/jsx-dev-runtime", "react-dom/client", "remotion", "@astrid/editor-sdk"})
# The esbuild JavaScript API is also executable code. Pin it in addition to the
# native binary supplied by the release lock (esbuild 0.27.7 npm distribution).
_API_HASH = "bce772de103e1cbb118f1b7313916caa81f0e85461b3b6e3531607e7ed75ec85"


def _error(pack_id: str, code: str, reason: str, fix: str) -> BuildResult:
    return {"version": 1, "ok": False, "pack_id": pack_id, "code": code,
            "reason": reason, "fix": fix, "diagnostics": []}


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "?" in value or "#" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and str(path) == value and all(p not in (".", "..") for p in path.parts)


def build_pack_assets(request: BuildRequest) -> BuildResult:
    """Build to the private stage and return a typed, JSON-safe result/error."""
    pack_id = request.get("pack_id", "") if isinstance(request, Mapping) else ""
    if not isinstance(pack_id, str):
        pack_id = ""
    invalid = lambda: _error(pack_id, "invalid_request", "Invalid build request.", "Supply the frozen version 1 request.")
    try:
        if not isinstance(request, Mapping) or set(request) != {"version", "transaction_id", "pack_id", "stage_root", "source_digest", "entry", "extension_manifest", "toolchain"}:
            return invalid()
        if type(request["version"]) is not int or request["version"] != 1 or not _ID.fullmatch(pack_id) or not isinstance(request["transaction_id"], str) or not request["transaction_id"] or not isinstance(request["source_digest"], str) or not _HASH.fullmatch(request["source_digest"]):
            return invalid()
        root = Path(request["stage_root"])
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            return invalid()
        entry, manifest = request["entry"], request["extension_manifest"]
        if entry is None and manifest is None:
            return {"version": 1, "ok": True, "pack_id": pack_id, "source_digest": request["source_digest"], "artifact_manifest": None}
        if not _relative(entry) or not entry.endswith(".tsx") or not isinstance(manifest, dict):
            return invalid()
        if not {"id", "version", "label", "apiVersion", "contributions"}.issubset(manifest) or not all(isinstance(manifest[k], str) and manifest[k] for k in ("id", "version", "label")) or type(manifest["apiVersion"]) is not int or manifest["apiVersion"] < 1 or not isinstance(manifest["contributions"], list):
            return invalid()
        source = root / entry
        if not source.is_file() or source.resolve() != source:
            return invalid()
        tool = request["toolchain"]
        fields = {"node_path", "node_version", "node_sha256", "esbuild_path", "esbuild_version", "esbuild_sha256", "lock_sha256", "shared_modules"}
        if not isinstance(tool, dict) or set(tool) != fields:
            return invalid()
        if not all(isinstance(tool[k], str) and _HASH.fullmatch(tool[k]) for k in ("node_sha256", "esbuild_sha256", "lock_sha256")) or not all(isinstance(tool[k], str) and _VERSION.fullmatch(tool[k]) for k in ("node_version", "esbuild_version")):
            return invalid()
        shared = tool["shared_modules"]
        if not isinstance(shared, dict) or set(shared) != _SINGLETONS:
            return _error(pack_id, "unsafe_import", "Singleton module lock is incomplete.", "Provide every approved singleton mapping.")
        for item in shared.values():
            if not isinstance(item, dict) or set(item) != {"url", "sha256", "version"} or not isinstance(item["url"], str) or not item["url"].startswith("/") or item["url"].startswith("//") or any(c in item["url"] for c in "?#%\\") or not _relative(item["url"][1:]) or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"]) or not isinstance(item["version"], str) or not _VERSION.fullmatch(item["version"]):
                return invalid()
        node, binary = Path(tool["node_path"]), Path(tool["esbuild_path"])
        api = binary.parent.parent / "lib" / "main.js"
        if not node.is_absolute() or not binary.is_absolute() or tool["esbuild_version"] != "0.27.7" or _sha(node) != tool["node_sha256"] or _sha(binary) != tool["esbuild_sha256"] or _sha(api) != _API_HASH:
            return _error(pack_id, "tool_mismatch", "Build tool does not match the release lock.", "Install the exact pinned local toolchain.")
        env = {"PATH": "", "LANG": "C.UTF-8", "ESBUILD_BINARY_PATH": str(binary)}
        if subprocess.run([str(node), "--version"], capture_output=True, text=True, env=env, timeout=15).stdout.strip() != "v" + tool["node_version"] or subprocess.run([str(binary), "--version"], capture_output=True, text=True, env=env, timeout=15).stdout.strip() != tool["esbuild_version"]:
            return _error(pack_id, "tool_mismatch", "Build tool version does not match the release lock.", "Install the exact pinned local toolchain.")
        child_request = dict(request)
        child_request["esbuild_api"] = str(api)
        result = subprocess.run([str(node), "-e", BUILDER_SOURCE], input=json.dumps(child_request, allow_nan=False), capture_output=True, text=True, env=env, timeout=120, cwd=root)
        if result.returncode != 0:
            return _error(pack_id, "compile_failed", "Builder failed.", "Check the pack source and build inputs.")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("pack_id") != pack_id or type(value.get("ok")) is not bool:
            return _error(pack_id, "compile_failed", "Builder returned an invalid result.", "Check the pinned builder installation.")
        if value["ok"]:
            artifact = value.get("artifact_manifest")
            if not isinstance(artifact, dict) or not isinstance(artifact.get("files"), list):
                return _error(pack_id, "incomplete_artifacts", "Builder omitted artifact files.", "Check the pinned builder.")
            seen: set[str] = set()
            for item in artifact["files"]:
                name = item.get("path") if isinstance(item, dict) else None
                if not _relative(name) or unicodedata.normalize("NFC", name) != name or name.casefold() in seen:
                    return _error(pack_id, "unsafe_output", "Builder emitted a colliding or unsafe path.", "Rename the conflicting files.")
                name.encode("utf-8", "strict")
                seen.add(name.casefold())
        return value
    except (OSError, subprocess.SubprocessError):
        return _error(pack_id, "io_failed", "Build input or tool could not be accessed.", "Check the stage and tool paths.")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return invalid()
