#!/usr/bin/env python3
"""Regenerate Agent and App Runtime pins from immutable Git commits.

The command defaults to a read-only preview. Pass ``--apply`` to copy the
verified Runtime artifacts and update both consumer locks and the App install
manifest. Runtime and Agent commits are selected by full SHA, never by checkout
HEAD or a mutable branch name.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# The packaged copy must stay byte-identical to the root lock (the single source).
PACKAGED_LOCK = "astrid/resources/runtime.lock.json"
SHA = re.compile(r"^[0-9a-f]{40}$")
SCHEMA_DIGEST = re.compile(r"^SCHEMA_DIGEST\s*=\s*['\"](sha256:[0-9a-f]{64})['\"]", re.M)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def checked_sha(repo: Path, value: str, label: str) -> str:
    if not SHA.fullmatch(value):
        raise ValueError(f"{label} must be a full lowercase 40-character commit SHA")
    resolved = git(repo, "rev-parse", "--verify", f"{value}^{{commit}}").decode().strip()
    if resolved != value:
        raise ValueError(f"{label} did not resolve to the supplied full commit SHA")
    return value


def read_at(repo: Path, commit: str, path: str) -> bytes:
    if (not isinstance(path, str) or not path or path.startswith("/")
            or "\\" in path or ":" in path
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError(f"unsafe repository path: {path!r}")
    return git(repo, "show", f"{commit}:{path}")


def json_at(repo: Path, commit: str, path: str) -> dict[str, Any]:
    value = json.loads(read_at(repo, commit, path))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def target_path(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or relative.startswith("/")
            or "\\" in relative or any(p in ("", ".", "..") for p in relative.split("/"))):
        raise ValueError(f"unsafe consumer path: {relative!r}")
    root = root.resolve()
    result = (root / relative).resolve()
    if result != root and root not in result.parents:
        raise ValueError(f"consumer path escapes checkout: {relative!r}")
    return result


def make_lock(old_lock: dict[str, Any], runtime_repo: Path, runtime_sha: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest_path = old_lock.get("manifest")
    if not isinstance(manifest_path, str):
        raise ValueError("runtime lock must name its producer manifest")
    manifest = json_at(runtime_repo, runtime_sha, manifest_path)
    producer = old_lock.get("producer")
    if manifest.get("producer") != producer:
        raise ValueError(f"producer identity mismatch in {manifest_path}")
    artifacts = manifest.get("artifacts")
    old_files = old_lock.get("files")
    if not isinstance(artifacts, dict) or not isinstance(old_files, dict) or not old_files:
        raise ValueError("runtime lock files and producer artifacts must be nonempty objects")
    files: dict[str, dict[str, str]] = {}
    contents: dict[str, bytes] = {}
    for consumer, entry in sorted(old_files.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("from"), str):
            raise ValueError(f"invalid Runtime lock entry for {consumer!r}")
        source = entry["from"]
        payload = read_at(runtime_repo, runtime_sha, source)
        actual = digest(payload)
        if artifacts.get(source) != actual:
            raise ValueError(f"Runtime artifact manifest is stale or missing {source}")
        target_path(ROOT, consumer)  # reject unsafe mappings before any writes
        files[consumer] = {"from": source, "sha256": actual}
        contents[consumer] = payload
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Runtime producer manifest version must be a nonempty string")
    lock = dict(old_lock)
    lock["files"] = files
    lock["producer_version"] = version
    lock["source_commit"] = runtime_sha
    return lock, contents


def build_outputs(agent_root: Path, app_root: Path, runtime_repo: Path,
                  runtime_sha: str, agent_sha: str) -> dict[Path, bytes]:
    runtime_lock_path = "runtime.lock.json"
    app_lock_path = "runtime.lock.json"
    agent_seed = json.loads((agent_root / runtime_lock_path).read_text(encoding="utf-8"))
    app_seed = json.loads((app_root / app_lock_path).read_text(encoding="utf-8"))
    agent_lock, agent_files = make_lock(agent_seed, runtime_repo, runtime_sha)
    app_lock, app_files = make_lock(app_seed, runtime_repo, runtime_sha)
    schema_meta = read_at(runtime_repo, runtime_sha, "runtime_protocol/contract_metadata.py").decode("utf-8")
    match = SCHEMA_DIGEST.search(schema_meta)
    if match is None:
        raise ValueError("Runtime generated contract metadata has no valid SCHEMA_DIGEST")

    install_path = app_root / "public/install-manifest.json"
    install_manifest = json.loads(install_path.read_text(encoding="utf-8"))
    if not isinstance(install_manifest, dict) or not isinstance(install_manifest.get("agent"), dict) or not isinstance(install_manifest.get("runtime"), dict):
        raise ValueError("App install manifest must contain agent and runtime objects")
    install_manifest["agent"]["sha"] = agent_sha
    install_manifest["runtime"]["sha"] = runtime_sha
    install_manifest["runtime"]["schema_digest"] = match.group(1)

    outputs: dict[Path, bytes] = {}
    for root, lock, files in ((agent_root, agent_lock, agent_files), (app_root, app_lock, app_files)):
        for consumer, payload in files.items():
            outputs[target_path(root, consumer)] = payload
        lock_bytes = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode()
        outputs[target_path(root, "runtime.lock.json")] = lock_bytes
        if root == agent_root:
            outputs[target_path(root, PACKAGED_LOCK)] = lock_bytes
    outputs[target_path(app_root, "public/install-manifest.json")] = (json.dumps(install_manifest, indent=2) + "\n").encode()
    return outputs


def preview(outputs: dict[Path, bytes]) -> None:
    for path, new_bytes in sorted(outputs.items(), key=lambda item: str(item[0])):
        old_bytes = path.read_bytes() if path.is_file() else b""
        if old_bytes == new_bytes:
            continue
        before = old_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        after = new_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        sys.stdout.writelines(difflib.unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}"))
    if all(path.is_file() and path.read_bytes() == value for path, value in outputs.items()):
        print("No changes.")


def apply(outputs: dict[Path, bytes]) -> None:
    # All Git objects, manifests, mappings, and hashes are checked before this
    # function is reached. Each file is written to a temp file beside it and moved
    # into place with os.replace, so a failure never leaves a half-written lock.
    for path, value in sorted(outputs.items(), key=lambda item: str(item[0])):
        if path.is_file() and path.read_bytes() == value:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_bytes(value)
            if path.is_file():
                os.chmod(temp, path.stat().st_mode & 0o7777)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        print(f"updated {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-sha", required=True, help="full Runtime commit SHA")
    parser.add_argument("--agent-sha", required=True, help="full Agent commit SHA for App manifest")
    parser.add_argument("--runtime-checkout", type=Path, required=True)
    parser.add_argument("--app-checkout", type=Path, required=True)
    parser.add_argument("--agent-checkout", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true", help="write verified outputs; default is a read-only diff")
    args = parser.parse_args(argv)
    try:
        runtime_repo, app_root, agent_root = (p.resolve() for p in (args.runtime_checkout, args.app_checkout, args.agent_checkout))
        runtime_sha = checked_sha(runtime_repo, args.runtime_sha, "Runtime SHA")
        agent_sha = checked_sha(agent_root, args.agent_sha, "Agent SHA")
        outputs = build_outputs(agent_root, app_root, runtime_repo, runtime_sha, agent_sha)
        if args.apply:
            apply(outputs)
        else:
            preview(outputs)
            print("Dry run only; no checkout files were written. Use --apply to write these changes.")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"error: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
