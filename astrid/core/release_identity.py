"""Portable, closed release identity observations for the Stage 1 boundary."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

SCHEMA_VERSION = "release-identity-v1"
NONE = "NONE"
CANDIDATE_COMPONENT_SCHEMA = "candidate-component-row-v1"
PRELIVE_MANIFEST_SCHEMA = "pre-live-manifest-v1"
CANDIDATE_COMPONENT_FIELDS = ("component_id", "repository_identity", "source_ref", "base_oid", "base_tree_oid", "integrated_oid", "integrated_tree_oid", "subtree_sha256", "contract_sha256", "generator_ids", "dependency_lock_digests", "fixture_digests", "tool_ids", "generator_observation_rows", "provenance_input_bindings", "producer_id", "epoch_profile_id", "freshness_policy_id")
PRELIVE_MANIFEST_FIELDS = ("schema_version", "governance_binding", "seed_ids", "evidence_rows", "excluded_ids", "epochs", "manifest_sha256")
CANDIDATE_CORE_FIELDS = ("schema_version", "governance_binding", "component_manifest_sha256", "contract_id", "runtime_build_id", "source_manifest_id", "migration_manifest_id", "selected_realm_id", "trusted_disposition_sha256", "pre_live_evidence_root", "contract_epoch", "runtime_epoch", "source_epoch", "migration_epoch", "activation_epoch", "release_epoch", "component_rows")
REMOTE_TARGET_FIELDS = ("remote_target_id", "target_kind", "component_id", "local_repository_identity", "repository_identity", "canonical_url", "destination_ref_or_prefix", "expected_old_oid", "reviewed_source_oid", "identity_transition_sha256", "repository_provision_receipt_rows")
PRELIVE_SEED_SOURCE = ("CURRENT-PLAN CURRENT-GOAL NORTH-STAR CUSTODY PHASE0-BASELINE GOVERNANCE-AMENDMENT THROUGHPUT-POLICY VALIDATOR-ID ROADMAP-OVERALL ROADMAP-ASTRID-BETA ROADMAP-HARDENING ROADMAP-VISION ROADMAP-README CONVERGENCE BUNDLE-MANIFEST EXECUTION-PACKETS EXECUTION-REQUIREMENTS EXECUTION-COVERAGE EXECUTION-VALIDATION-MATRIX EXECUTION-COMMANDS EXECUTION-INTEGRATIONS EXECUTION-COMPONENTS EXECUTION-SCHEMAS-MANIFEST EXECUTION-VECTORS-MANIFEST BUNDLE-B0 RCPT-B0-MATERIALIZE P(B0.1) P(B0.2) P(B0.3) G(K-B0) CONTRACT-ID RUNTIME-BUILD-ID SOURCE-MANIFEST-ID MIGRATION-MANIFEST-ID SELECTED-REALM-ID TRUSTED-DISPOSITION-SHA256 RCPT-REV-C1 RCPT-REV-C2 RCPT-REV-C3 RCPT-REV-C4 G(K-B10) P(B11.1) P(B11.2) REVIEWED-COMPONENTS-B11 REMOTE-TARGET-LOCATORS REMOTE-TARGET-SET").split()
PRELIVE_EXCLUDED_IDS = ("PRELIVE-MANIFEST", "RCPT-PRELIVE-MANIFEST", "PRELIVE-ROOT", "RCPT-IDENTITY-PRELIVE", "CANDIDATE-CORE", "RCPT-IDENTITY-CANDIDATE-CORE")
PRELIVE_SEEDS = tuple(sorted(set(PRELIVE_SEED_SOURCE)))
PRELIVE_SEED_COUNT = len(PRELIVE_SEEDS)
EVIDENCE_ARTIFACT_FIELDS = ("schema_version", "artifact_id", "artifact_kind", "producer_id", "governance_binding", "input_bindings", "canonical_content_base64", "byte_length", "content_sha256", "media_type", "detail_schema_id")
GENERATOR_ROW_FIELDS = ("schema_version", "row_kind", "generator_id", "component_id", "entrypoint_component_id", "entrypoint_path", "entrypoint_sha256", "interpreter_tool_id", "argv_formula_id", "sandbox_policy_id", "generator_definition_sha256", "input_schema_ids", "input_digests", "declared_output_roots", "tool_ids", "output_paths", "output_digests", "tool_rows", "run_ordinal", "argv_carrier", "argv_sha256", "clean_checkout_id", "changed_paths", "undeclared_changed_paths", "started_at", "finished_at", "exit_code", "stop_class", "first_run_receipt_sha256", "second_run_receipt_sha256", "run_receipt_evidence_rows", "provenance_input_bindings", "producer_id")
GENERATOR_RECEIPT_FIELDS = ("schema_version", "artifact_kind", "generator_id", "run_ordinal", "argv", "argv_sha256", "output_rows", "exit_code")
RECEIPT_ROOT_ENVIRONMENTS = ("ASTRID_RELEASE_RECEIPT_ROOT", "RELEASE_RECEIPT_ROOT")

class ReleaseIdentityError(ValueError):
    """A release identity cannot be produced or verified safely."""

def _nfc(value: Any) -> Any:
    if isinstance(value, str): return unicodedata.normalize("NFC", value)
    if isinstance(value, list): return [_nfc(v) for v in value]
    if isinstance(value, tuple): return [_nfc(v) for v in value]
    if isinstance(value, dict): return {unicodedata.normalize("NFC", str(k)): _nfc(v) for k, v in value.items()}
    return value

def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_nfc(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

def framed_hash(label: str, value: Any) -> str:
    left, right = unicodedata.normalize("NFC", label).encode("utf-8"), canonical_bytes(value)
    return hashlib.sha256(len(left).to_bytes(8, "big") + left + len(right).to_bytes(8, "big") + right).hexdigest()

def _sha256_bytes(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def _artifact_wrapper(artifact_id: str, producer_id: str, content: bytes, *, media_type: str = "application/json", detail_schema_id: str = "evidence-artifact-v1", input_bindings: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {"schema_version": 1, "artifact_id": artifact_id, "artifact_kind": "evidence-artifact", "producer_id": producer_id, "governance_binding": "LOCAL-STAGE1-RELEASE", "input_bindings": list(input_bindings), "canonical_content_base64": base64.b64encode(content).decode("ascii"), "byte_length": len(content), "content_sha256": _sha256_bytes(content), "media_type": media_type, "detail_schema_id": detail_schema_id}

def _git(path: Path, *argv: str, text: bool = True, optional: bool = False) -> str | bytes:
    try:
        result = subprocess.run(["git", "-C", str(path), *argv], capture_output=True, text=text, check=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if optional: return "" if text else b""
        raise ReleaseIdentityError(f"git observation failed for {path}: {' '.join(argv)}") from exc
    return result.stdout

def _git_text(path: Path, *argv: str, optional: bool = False) -> str:
    return str(_git(path, *argv, text=True, optional=optional)).rstrip("\n")

def _git_bytes(path: Path, *argv: str) -> bytes: return bytes(_git(path, *argv, text=False))

def _tracked_names(path: Path) -> list[str]:
    return [item.decode("utf-8") for item in _git_bytes(path, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0") if item]

def _repo_identity(path: Path) -> str:
    remote = _git_text(path, "config", "--get", "remote.origin.url", optional=True)
    if not remote: return path.name
    match = re.search(r"(?:github\.com[:/])([^/ :]+/[^/]+?)(?:\.git)?$", remote)
    identity = match.group(1) if match else remote.removesuffix(".git")
    return "astrid-runtime-oracle" if identity == "banodoco/astrid-runtime" else identity

def git_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Portable worktree/common-dir/detached metadata plus recursive submodules."""
    root = Path(path).expanduser().resolve()
    git_dir = _git_text(root, "rev-parse", "--git-dir")
    common = _git_text(root, "rev-parse", "--git-common-dir")
    ref = _git_text(root, "symbolic-ref", "-q", "--short", "HEAD", optional=True)
    subs = []
    for line in _git_text(root, "submodule", "status", "--recursive", optional=True).splitlines():
        match = re.match(r"^[ +-]?([0-9a-f]{40,64})\s+([^ (]+)", line)
        if match:
            subpath, subroot = match.group(2), root / match.group(2)
            initialized = subroot.is_dir() and (subroot / ".git").exists()
            subref = _git_text(subroot, "symbolic-ref", "-q", "--short", "HEAD", optional=True) if initialized else ""
            subgit = _git_text(subroot, "rev-parse", "--git-dir", optional=True); subcommon = _git_text(subroot, "rev-parse", "--git-common-dir", optional=True)
            subs.append({"path": subpath, "oid": match.group(1), "head_ref": subref or NONE, "detached": not bool(subref), "git_dir_relative": "WORKTREE" if (subroot / ".git").is_file() else "MAIN", "common_dir_relative": ".", "dirty_paths": _dirty_paths(subroot) if initialized else []})
    return {"repository_identity": _repo_identity(root), "head_oid": _git_text(root, "rev-parse", "HEAD"), "head_ref": ref or NONE, "detached": not bool(ref), "git_dir_kind": "worktree" if (root / ".git").is_file() else "directory", "git_dir_relative": "WORKTREE" if (root / ".git").is_file() else "MAIN", "common_dir_relative": ".", "submodules": sorted(subs, key=lambda x: x["path"])}

def _submodule_sources(root: Path, approved_root: Path) -> list[dict[str, str]]:
    lines = _git_text(root, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.url$", optional=True).splitlines()
    result = []
    for line in lines:
        key, url = line.split(None, 1); name = key.removeprefix("submodule.").removesuffix(".url"); parsed = urlparse(url)
        if parsed.scheme in {"", "file"}:
            candidate = Path(parsed.path if parsed.scheme == "file" else url)
            if not candidate.is_absolute(): candidate = root / candidate
            resolved = candidate.resolve()
            try: resolved.relative_to(approved_root)
            except ValueError as exc: raise ReleaseIdentityError("local submodule URL escapes the approved source root") from exc
            result.append({"name": name, "url": resolved.as_uri()})
        elif parsed.scheme not in {"https", "ssh", "git"}: raise ReleaseIdentityError("submodule URL scheme is not permitted")
    return result

def _dirty_paths(path: Path, *, exclude: str | os.PathLike[str] | None = None) -> list[str]:
    raw = _git_text(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
    excluded = None
    if exclude:
        try:
            excluded = Path(exclude).expanduser().resolve().relative_to(path.resolve()).as_posix()
        except ValueError:
            pass
    found = []
    for line in raw.splitlines():
        if not line:
            continue
        value = line[3:] if len(line) >= 3 else line
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        if not excluded or value != excluded:
            found.append(value)
    return sorted(set(found))


def _inventory_digest(path: Path, names: Iterable[str]) -> str:
    rows = []
    for name in sorted(set(names)):
        oid = _git_text(path, "rev-parse", f"HEAD:{name}", optional=True)
        if oid:
            rows.append({"path": name, "oid": oid})
    return framed_hash("astrid_runtime.release-inventory.v1", rows)


def _scope(paths: Sequence[str], needles: Sequence[str]) -> list[str]:
    return [p for p in paths if any(n in p.lower() for n in needles)]


def _candidate_shape(row: Mapping[str, Any]) -> dict[str, Any]:
    if set(row) != set(CANDIDATE_COMPONENT_FIELDS):
        raise ReleaseIdentityError("candidate-component-row-v1 has unexpected or missing fields")
    return {k: _nfc(row[k]) for k in CANDIDATE_COMPONENT_FIELDS}


def resolve_component(
    component_id: str,
    path: str | os.PathLike[str],
    *,
    source_ref: str | None = None,
    scope_paths: Sequence[str] | None = None,
    epochs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(component_id, str) or not component_id:
        raise ReleaseIdentityError("component_id must be a non-empty string")
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ReleaseIdentityError(f"candidate component is not a Git checkout: {root}")
    names = _tracked_names(root)
    scope = sorted(set(scope_paths or names))
    if set(scope) - set(names):
        raise ReleaseIdentityError(f"scope paths are not in HEAD for {component_id}")
    head, tree = _git_text(root, "rev-parse", "HEAD"), _git_text(root, "rev-parse", "HEAD^{tree}")
    contract = _scope(names, ("contract/", "schema", "openapi", "conformance/"))
    generated = _scope(names, ("generated", "/client", "client/"))
    capabilities = _scope(names, ("capabil", "manifest", "pack/"))
    dependency = _scope(names, ("lock", "requirements", "pyproject.toml", "package.json"))
    fixtures = _scope(names, ("fixture", "fixtures"))
    _ = epochs
    _ = capabilities
    ref = _git_text(root, "symbolic-ref", "-q", "HEAD", optional=True) or head
    if source_ref is not None and not (
        re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", source_ref)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_ref)
    ):
        raise ReleaseIdentityError("source_ref must be a full branch ref or detached object ID")
    identity_bytes = canonical_bytes(git_identity(root))
    identity_binding = {
        "input_id": f"GIT-IDENTITY:{component_id}",
        "sha256": _sha256_bytes(identity_bytes),
    }
    return _candidate_shape(
        {
            "component_id": component_id,
            "repository_identity": _repo_identity(root),
            "source_ref": source_ref or ref,
            "base_oid": head,
            "base_tree_oid": tree,
            "integrated_oid": head,
            "integrated_tree_oid": tree,
            "subtree_sha256": framed_hash(
                "astrid_runtime.component-subtree.v1",
                [{"path": p, "oid": _git_text(root, "rev-parse", f"HEAD:{p}")} for p in scope],
            ),
            "contract_sha256": _inventory_digest(root, contract),
            "generator_ids": generated,
            "dependency_lock_digests": [
                {"path": p, "sha256": _sha256_bytes(_git_bytes(root, "show", f"HEAD:{p}"))}
                for p in dependency
            ],
            "fixture_digests": [
                {"path": p, "sha256": _sha256_bytes(_git_bytes(root, "show", f"HEAD:{p}"))}
                for p in fixtures
            ],
            "tool_ids": ["TOOL-GIT"],
            "generator_observation_rows": [],
            "provenance_input_bindings": [identity_binding],
            "producer_id": "PROD-CMD-PACKET:B11.1",
            "epoch_profile_id": "EP-CRSM",
            "freshness_policy_id": "CURRENT-CLEAN-HEAD",
        }
    )


def resolve_reviewed_components(
    components: Mapping[str, str | os.PathLike[str]], **kwargs: Any
) -> list[dict[str, Any]]:
    if len(set(components)) != len(components):
        raise ReleaseIdentityError("component IDs must be unique")
    return sorted(
        (resolve_component(cid, path, **kwargs) for cid, path in components.items()),
        key=lambda row: row["component_id"],
    )


def _assert_clean(
    components: Mapping[str, str | os.PathLike[str]], output: str | os.PathLike[str] | None = None
) -> None:
    if output is not None:
        target = Path(output).expanduser().resolve()
        for path in components.values():
            root = Path(path).expanduser().resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            raise ReleaseIdentityError("receipt output may not be inside a component checkout")
    dirty = []
    for cid, path in components.items():
        paths = _dirty_paths(Path(path).expanduser().resolve())
        if paths:
            dirty.append((cid, paths))
    if dirty:
        raise ReleaseIdentityError(f"candidate component has uncommitted changes: {dirty}")


def _plan_transition(
    cid: str, local: str, canonical: str, url: str, ref: str, reviewed_source_oid: str = NONE
) -> str:
    return framed_hash(
        "astrid_runtime.local-to-canonical-repository.v1",
        [cid, local, canonical, url, ref, reviewed_source_oid],
    )


def plan_component_registry() -> list[dict[str, Any]]:
    rows = [
        (
            "NEUTRAL-RUNTIME",
            "astrid-runtime-oracle",
            "banodoco/astrid-runtime",
            "https://github.com/banodoco/astrid-runtime.git",
        ),
        (
            "ASTRID-CLIENT",
            "banodoco/astrid-agent",
            "banodoco/astrid-agent",
            "https://github.com/banodoco/astrid-agent.git",
        ),
    ]
    return [
        _nfc(
            {
                "remote_target_id": f"REMOTE-TARGET:COMPONENT:{cid}",
                "target_kind": "component",
                "component_id": cid,
                "local_repository_identity": local,
                "repository_identity": canonical,
                "canonical_url": url,
                "destination_ref_or_prefix": "refs/heads/main",
                "expected_old_oid": NONE,
                "reviewed_source_oid": NONE,
                "identity_transition_sha256": _plan_transition(
                    cid, local, canonical, url, "refs/heads/main"
                ),
                "repository_provision_receipt_rows": NONE,
            }
        )
        for cid, local, canonical, url in rows
    ]


PLAN_COMPONENT_REGISTRY = tuple(plan_component_registry())


def plan_publication_row() -> dict[str, Any]:
    return {
        "remote_target_id": "REMOTE-TARGET:PUBLICATION",
        "target_kind": "publication",
        "component_id": NONE,
        "local_repository_identity": NONE,
        "repository_identity": "banodoco/astrid-runtime",
        "canonical_url": "https://github.com/banodoco/astrid-runtime.git",
        "destination_ref_or_prefix": "refs/tags/astrid-stage1-evidence/",
        "expected_old_oid": NONE,
        "reviewed_source_oid": NONE,
        "identity_transition_sha256": NONE,
        "repository_provision_receipt_rows": NONE,
    }


def component_registry_sha256(rows: Sequence[Mapping[str, Any]] | None = None) -> str:
    return _sha256_bytes(
        canonical_bytes(list(rows if rows is not None else plan_component_registry()))
    )


def _directory_inventory(root: Path) -> list[dict[str, str]]:
    rows = []
    if not root.is_dir():
        raise ReleaseIdentityError("generator staging root is missing")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            raise ReleaseIdentityError("generator output contains a non-regular entry")
        data = path.read_bytes()
        rows.append({"path": relative, "sha256": _sha256_bytes(data), "byte_length": len(data)})
    return rows


def _filesystem_snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Hash every input entry without following links, including Git metadata."""
    result: dict[str, tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", _sha256_bytes(path.read_bytes()))
    return result


def _remove_git_remotes(root: Path) -> None:
    for remote in _git_text(root, "remote", optional=True).splitlines():
        if remote:
            subprocess.run(
                ["git", "-C", str(root), "remote", "remove", remote], check=True, timeout=30
            )


def _clean_git_checkout(
    source: Path, destination: Path, expected_oid: str, approved_root: Path | None = None
) -> None:
    source_identity = git_identity(source)
    local_submodules = _submodule_sources(source, (approved_root or source.parent).resolve())
    result = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise ReleaseIdentityError("B11.1 could not create a clean pinned checkout")
    # A generator must not retain an origin it can push to or use to discover
    # the reviewed source checkout.  Inputs are copied into the run separately.
    _remove_git_remotes(destination)
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--quiet", "--detach", "HEAD"],
        check=True,
        timeout=30,
    )
    if _git_text(destination, "rev-parse", "HEAD") != expected_oid:
        raise ReleaseIdentityError("B11.1 clone is not pinned to integrated_oid")
    for item in local_submodules:
        subprocess.run(
            ["git", "-C", str(destination), "config", f"submodule.{item['name']}.url", item["url"]],
            check=True,
            timeout=30,
        )
    if (
        local_submodules
        and subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--recursive",
            ],
            check=False,
            timeout=120,
        ).returncode
        != 0
    ):
        raise ReleaseIdentityError("B11.1 local submodule initialization failed")
    for item in source_identity["submodules"]:
        clone_sub = destination / item["path"]
        if item["head_ref"] != NONE and clone_sub.is_dir():
            subprocess.run(
                ["git", "-C", str(clone_sub), "checkout", "--quiet", item["head_ref"]],
                check=True,
                timeout=30,
            )
        if clone_sub.is_dir():
            _remove_git_remotes(clone_sub)
    clone_identity = git_identity(destination)
    if [
        {k: x[k] for k in ("path", "oid", "head_ref", "detached", "dirty_paths")}
        for x in clone_identity.get("submodules", [])
    ] != [
        {k: x[k] for k in ("path", "oid", "head_ref", "detached", "dirty_paths")}
        for x in source_identity.get("submodules", [])
    ]:
        raise ReleaseIdentityError(
            "B11.1 clone recursive submodule identity differs from reviewed source"
        )
    if _dirty_paths(destination):
        raise ReleaseIdentityError("B11.1 recursive checkout is not clean")


def _sandbox_command(argv: list[str], *, staging: Path) -> list[str]:
    """Run a generator in the current-Mac write boundary."""
    if sys.platform != "darwin":
        raise ReleaseIdentityError("B11.1 requires the current-Mac filesystem sandbox")
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        raise ReleaseIdentityError("B11.1 requires sandbox-exec")
    escaped = str(staging.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    profile = (
        '(version 1)\n(deny default)\n(allow process-exec)\n(allow process-fork)\n(allow sysctl-read)\n(allow file-read*)\n(allow file-write* (subpath "'
        + escaped
        + '"))\n(allow file-write* (literal "/dev/null"))\n'
    )
    return [sandbox_exec, "-p", profile, *argv]


def run_b11_1(
    component_rows: Sequence[Mapping[str, Any]],
    generator_definitions: Sequence[Mapping[str, Any]],
    *,
    contract_bytes: bytes,
    schema_manifest_bytes: bytes,
    component_manifest_bytes: bytes | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Execute each declared B11.1 generator twice and attach observations.

    Commands are direct argv only.  Each run receives a fresh staging root and
    the complete input bytes; the two byte inventories must match exactly.
    """
    rows = [_candidate_shape(row) for row in component_rows]
    by_component = {row["component_id"]: row for row in rows}
    definitions = sorted(
        (dict(defn) for defn in generator_definitions), key=lambda d: d.get("generator_id", "")
    )
    if not definitions:
        raise ReleaseIdentityError("B11.1 requires declared generator definitions")
    observed: dict[str, list[dict[str, Any]]] = {cid: [] for cid in by_component}
    with tempfile.TemporaryDirectory(dir=str(output_root) if output_root else None) as temp:
        base = Path(temp)
        for definition in definitions:
            gid = definition.get("generator_id")
            cid = definition.get("component_id")
            if not isinstance(gid, str) or not isinstance(cid, str) or cid not in by_component:
                raise ReleaseIdentityError("B11.1 generator is not bound to a reviewed component")
            entrypoint_path = str(definition.get("entrypoint_path", ""))
            entrypoint_relative = Path(entrypoint_path)
            if entrypoint_relative.is_absolute() or ".." in entrypoint_relative.parts:
                raise ReleaseIdentityError("B11.1 entrypoint path must be relative and contained")
            source_checkout = Path(definition.get("checkout", "")).expanduser().resolve()
            if _dirty_paths(source_checkout):
                raise ReleaseIdentityError("B11.1 source checkout is not clean")
            entrypoint = source_checkout / entrypoint_path
            if not entrypoint.is_file() or entrypoint.is_symlink():
                raise ReleaseIdentityError("B11.1 generator entrypoint is not a regular file")
            entrypoint_digest = _sha256_bytes(entrypoint.read_bytes())
            inventories = []
            receipts = []
            for ordinal in (1, 2):
                source_snapshot = _filesystem_snapshot(source_checkout)
                source_status = _dirty_paths(source_checkout)
                run_root = base / gid / str(ordinal)
                checkout = run_root / "checkout"
                _clean_git_checkout(
                    source_checkout,
                    checkout,
                    by_component[cid]["integrated_oid"],
                    Path(output_root).expanduser().resolve() if output_root else None,
                )
                stage = run_root / "staging"
                inputs = run_root / "inputs"
                stage.mkdir(parents=True)
                inputs.mkdir()
                contract_path, schema_path = (
                    inputs / "contract.json",
                    inputs / "schema-manifest.json",
                )
                contract_path.write_bytes(contract_bytes)
                schema_path.write_bytes(schema_manifest_bytes)
                input_digests = {
                    contract_path: _sha256_bytes(contract_bytes),
                    schema_path: _sha256_bytes(schema_manifest_bytes),
                }
                component_path = None
                if component_manifest_bytes is not None:
                    component_path = inputs / "component-manifest.json"
                    component_path.write_bytes(component_manifest_bytes)
                    input_digests[component_path] = _sha256_bytes(component_manifest_bytes)
                checkout_snapshot = _filesystem_snapshot(checkout)
                executable = str(
                    definition.get("interpreter_path") or definition.get("executable") or "python3"
                )
                argv = [
                    executable,
                    str(checkout / entrypoint_path),
                    "--contract",
                    str(contract_path),
                    "--schema-manifest",
                    str(schema_path),
                ]
                if component_path is not None:
                    argv.extend(["--component-manifest", str(component_path)])
                argv.extend(["--output-root", str(stage)])
                before = _dirty_paths(checkout)
                result = subprocess.run(
                    _sandbox_command(argv, staging=stage),
                    cwd=str(checkout),
                    capture_output=True,
                    check=False,
                    timeout=300,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                    },
                )
                after = _dirty_paths(checkout)
                if (
                    _filesystem_snapshot(source_checkout) != source_snapshot
                    or _dirty_paths(source_checkout) != source_status
                ):
                    raise ReleaseIdentityError(
                        "B11.1 generator mutated the reviewed source checkout"
                    )
                if _filesystem_snapshot(checkout) != checkout_snapshot or before != after:
                    raise ReleaseIdentityError("B11.1 generator changed its clean pinned checkout")
                for path, digest in input_digests.items():
                    if not path.is_file() or _sha256_bytes(path.read_bytes()) != digest:
                        raise ReleaseIdentityError(
                            "B11.1 generator changed its contract, schema, or component-manifest input"
                        )
                if result.returncode != 0:
                    raise ReleaseIdentityError(f"B11.1 generator failed: {gid}")
                inventory = _directory_inventory(stage)
                if not inventory:
                    raise ReleaseIdentityError("B11.1 generator produced no output")
                inventories.append(inventory)
                stable_argv = [
                    "<interpreter>",
                    "<component-checkout>/" + str(definition["entrypoint_path"]),
                    "--contract",
                    "<contract-input>",
                    "--schema-manifest",
                    "<schema-manifest-input>",
                ]
                if component_path is not None:
                    stable_argv.extend(["--component-manifest", "<component-manifest-input>"])
                stable_argv.extend(["--output-root", "<staging-output-root>"])
                receipt = {
                    "schema_version": 1,
                    "artifact_kind": "generator-run-receipt",
                    "generator_id": gid,
                    "run_ordinal": ordinal,
                    "argv": stable_argv,
                    "argv_sha256": framed_hash("astrid_runtime.generator-run-argv.v1", stable_argv),
                    "output_rows": inventory,
                    "exit_code": result.returncode,
                }
                receipts.append(receipt)
            if inventories[0] != inventories[1]:
                raise ReleaseIdentityError("B11.1 generator runs are not byte-identical")
            committed_root = definition.get("committed_output_root")
            if committed_root:
                committed = _directory_inventory(source_checkout / str(committed_root))
                normalized = [{**item, "path": item["path"]} for item in committed]
                if inventories[0] != normalized:
                    raise ReleaseIdentityError(
                        "B11.1 staging inventory differs from committed generated-root inventory"
                    )
            output_paths = [item["path"] for item in inventories[0]]
            output_digests = [item["sha256"] for item in inventories[0]]
            definition_digest = _sha256_bytes(canonical_bytes(definition))
            receipt_rows = []
            for receipt in receipts:
                raw = canonical_bytes(receipt)
                receipt_rows.append(
                    _artifact_wrapper(
                        f"GENERATOR-RUN:{gid}:{receipt['run_ordinal']}",
                        "PROD-CMD-PACKET:B11.1",
                        raw,
                        detail_schema_id="generator-run-receipt-v1",
                    )
                )
            contract_digest = _sha256_bytes(contract_bytes)
            schema_digest = _sha256_bytes(schema_manifest_bytes)
            observation_input_digests = [contract_digest, schema_digest]
            observation_bindings = [
                {"input_id": "CONTRACT-ID", "sha256": contract_digest},
                {"input_id": "EXECUTION-SCHEMAS-MANIFEST", "sha256": schema_digest},
            ]
            if component_manifest_bytes is not None:
                component_digest = _sha256_bytes(component_manifest_bytes)
                observation_input_digests.append(component_digest)
                observation_bindings.append(
                    {"input_id": "COMPONENT-MANIFEST", "sha256": component_digest}
                )
            observation_bindings.append(
                {"input_id": "GENERATOR-DEFINITION", "sha256": definition_digest}
            )
            observation = {
                "schema_version": 1,
                "row_kind": "OBSERVATION",
                "generator_id": gid,
                "component_id": cid,
                "entrypoint_component_id": cid,
                "entrypoint_path": str(definition["entrypoint_path"]),
                "entrypoint_sha256": entrypoint_digest,
                "interpreter_tool_id": definition.get("interpreter_tool_id", "TOOL-PYTHON"),
                "argv_formula_id": "GENERATOR-ARGV-V1",
                "sandbox_policy_id": "GENERATOR-READONLY-STAGING-V1",
                "generator_definition_sha256": definition_digest,
                "input_schema_ids": list(definition.get("input_schema_ids", [])),
                "input_digests": observation_input_digests,
                "declared_output_roots": list(definition.get("declared_output_roots", ["."])),
                "tool_ids": list(definition.get("tool_ids", ["TOOL-GIT", "TOOL-PYTHON"])),
                "output_paths": output_paths,
                "output_digests": output_digests,
                "tool_rows": list(definition.get("tool_rows", [])),
                "run_ordinal": NONE,
                "argv_carrier": NONE,
                "argv_sha256": NONE,
                "clean_checkout_id": NONE,
                "changed_paths": [],
                "undeclared_changed_paths": [],
                "started_at": NONE,
                "finished_at": NONE,
                "exit_code": NONE,
                "stop_class": NONE,
                "first_run_receipt_sha256": _sha256_bytes(canonical_bytes(receipts[0])),
                "second_run_receipt_sha256": _sha256_bytes(canonical_bytes(receipts[1])),
                "run_receipt_evidence_rows": receipt_rows,
                "provenance_input_bindings": observation_bindings,
                "producer_id": "PROD-CMD-PACKET:B11.1",
            }
            if set(observation) != set(GENERATOR_ROW_FIELDS):
                raise ReleaseIdentityError("generator observation schema drift")
            observed[cid].append(observation)
    return [
        {
            **row,
            "generator_observation_rows": sorted(
                observed.get(row["component_id"], []), key=lambda item: item["generator_id"]
            ),
            "generator_ids": [
                item["generator_id"]
                for item in sorted(
                    observed.get(row["component_id"], []), key=lambda item: item["generator_id"]
                )
            ],
        }
        for row in rows
    ]


execute_b11_1 = run_b11_1


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", parsed.path)
    ):
        raise ReleaseIdentityError(
            "canonical URL must be an absolute credential-free HTTPS GitHub URL"
        )


def _validate_locator(value: Any) -> None:
    if not isinstance(value, str):
        raise ReleaseIdentityError("remote locator URL must be a string")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
    ):
        raise ReleaseIdentityError("remote locator must be credential-free HTTPS without traversal")


def join_plan_remote_targets(
    component_rows: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
    registry_rows: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = [_candidate_shape(r) for r in component_rows]
    if len({r["component_id"] for r in rows}) != len(rows):
        raise ReleaseIdentityError("plan join rejects duplicate component IDs before indexing")
    registry = [
        dict(r) for r in (registry_rows if registry_rows is not None else plan_component_registry())
    ]
    if registry_rows is not None and _sha256_bytes(
        canonical_bytes(registry)
    ) != component_registry_sha256(plan_component_registry()):
        raise ReleaseIdentityError("external plan registry digest mismatch")
    if (
        len(registry) != 2
        or len({r.get("remote_target_id") for r in registry}) != len(registry)
        or len({r.get("component_id") for r in registry}) != len(registry)
        or {r.get("component_id") for r in registry} != {"ASTRID-CLIENT", "NEUTRAL-RUNTIME"}
        or any(set(r) != set(REMOTE_TARGET_FIELDS) for r in registry)
    ):
        raise ReleaseIdentityError("plan registry rows are not exact, unique, and cardinality-two")
    by_id = {r["component_id"]: r for r in rows}
    if strict and set(by_id) != {r["component_id"] for r in registry}:
        raise ReleaseIdentityError("plan-owned component registry join is not total")
    result = []
    for target in registry:
        source = by_id.get(target["component_id"])
        if source is None or source["repository_identity"] != target["local_repository_identity"]:
            raise ReleaseIdentityError("local repository identity does not match plan registry")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["integrated_oid"]):
            raise ReleaseIdentityError("reviewed_source_oid must be a full Git object ID")
        _validate_url(target["canonical_url"])
        if target["identity_transition_sha256"] != _plan_transition(
            target["component_id"],
            target["local_repository_identity"],
            target["repository_identity"],
            target["canonical_url"],
            target["destination_ref_or_prefix"],
            NONE,
        ):
            raise ReleaseIdentityError("plan registry identity transition mismatch")
        item = copy.deepcopy(target)
        item["reviewed_source_oid"] = source["integrated_oid"]
        item["identity_transition_sha256"] = _plan_transition(
            item["component_id"],
            item["local_repository_identity"],
            item["repository_identity"],
            item["canonical_url"],
            item["destination_ref_or_prefix"],
            item["reviewed_source_oid"],
        )
        result.append(item)
    result.append(plan_publication_row())
    return result


def _seed_info(seed: str, metadata: Mapping[str, Any] | None) -> tuple[str, str]:
    item = ((metadata or {}).get("seed_artifacts", {}) or {}).get(seed, {})
    if not isinstance(item, Mapping):
        item = {}
    producer, media = (
        item.get("producer_id", "CMD-PRELIVE-MANIFEST"),
        item.get("media_type", "application/json"),
    )
    if not isinstance(producer, str) or not isinstance(media, str) or not producer or not media:
        raise ReleaseIdentityError("seed media/producer definitions must be non-empty strings")
    return producer, media


def build_prelive_manifest(
    seed_outputs: Mapping[str, bytes | bytearray | Mapping[str, Any] | Sequence[Any]] | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if seed_outputs == {}:
        raise ReleaseIdentityError("PRELIVE-MANIFEST is missing required seed bytes")
    if seed_outputs is None:
        raise ReleaseIdentityError("PRELIVE-MANIFEST requires actual seed bytes")
    outputs = seed_outputs
    seeds = list(PRELIVE_SEEDS)
    epochs = dict(
        (metadata or {}).get(
            "epochs",
            {
                "contract_epoch": NONE,
                "runtime_epoch": NONE,
                "source_epoch": NONE,
                "migration_epoch": NONE,
                "activation_epoch": NONE,
                "release_epoch": NONE,
            },
        )
    )
    evidence = []
    if set(outputs) != set(seeds):
        raise ReleaseIdentityError(
            f"PRELIVE-MANIFEST seed output set is not exactly {PRELIVE_SEED_COUNT} seeds"
        )
    for seed in seeds:
        value = outputs[seed]
        if not isinstance(value, (bytes, bytearray)):
            raise ReleaseIdentityError("PRELIVE seed outputs must be complete bytes")
        data = bytes(value)
        digest = _sha256_bytes(data)
        producer, media = _seed_info(seed, metadata)
        evidence.append(
            {
                "path": f"evidence/sha256/{digest[:2]}/{digest}",
                "sha256": digest,
                "producer_id": producer,
                "token_ids": [seed],
                "epochs": _nfc(epochs),
                "media_type": media,
            }
        )
    evidence.sort(key=lambda r: (r["path"], r["sha256"], r["producer_id"]))
    manifest = {
        "schema_version": PRELIVE_MANIFEST_SCHEMA,
        "governance_binding": "LOCAL-STAGE1-RELEASE",
        "seed_ids": seeds,
        "evidence_rows": evidence,
        "excluded_ids": list(PRELIVE_EXCLUDED_IDS),
        "epochs": _nfc(epochs),
    }
    manifest["manifest_sha256"] = framed_hash("astrid_runtime.pre-live-manifest.v1", manifest)
    if set(manifest) != set(PRELIVE_MANIFEST_FIELDS):
        raise ReleaseIdentityError("pre-live manifest schema drift")
    return manifest


def _seed_payload_wrappers(
    seed_outputs: Mapping[str, bytes | bytearray], metadata: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    if set(seed_outputs) != set(PRELIVE_SEEDS):
        raise ReleaseIdentityError(
            f"PRELIVE seed payload set is not exactly {PRELIVE_SEED_COUNT} seeds"
        )
    wrappers = []
    for seed in PRELIVE_SEEDS:
        value = seed_outputs[seed]
        if not isinstance(value, (bytes, bytearray)):
            raise ReleaseIdentityError("PRELIVE seed outputs must be complete bytes")
        content = bytes(value)
        producer, media = _seed_info(seed, metadata)
        wrappers.append(
            _artifact_wrapper(
                f"PRELIVE-SEED:{seed}",
                producer,
                content,
                media_type=media,
                detail_schema_id="pre-live-seed-v1",
            )
        )
    return wrappers


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    return framed_hash(
        "astrid_runtime.release-receipt.v1",
        {k: v for k, v in receipt.items() if k not in {"receipt_sha256", "identity"}},
    )


def _decode_artifact(
    item: Mapping[str, Any],
    *,
    artifact_id: str | None = None,
    producer_id: str | None = None,
    media_type: str | None = None,
) -> bytes:
    if (
        set(item) != set(EVIDENCE_ARTIFACT_FIELDS)
        or item.get("schema_version") != 1
        or item.get("artifact_kind") != "evidence-artifact"
        or item.get("governance_binding") != "LOCAL-STAGE1-RELEASE"
        or not isinstance(item.get("input_bindings"), list)
        or not isinstance(item.get("detail_schema_id"), str)
        or item.get("detail_schema_id") == NONE
    ):
        raise ReleaseIdentityError("evidence-artifact-v1 wrapper schema mismatch")
    if artifact_id is not None and item.get("artifact_id") != artifact_id:
        raise ReleaseIdentityError("evidence-artifact identity mismatch")
    if producer_id is not None and item.get("producer_id") != producer_id:
        raise ReleaseIdentityError("evidence-artifact producer mismatch")
    if media_type is not None and item.get("media_type") != media_type:
        raise ReleaseIdentityError("evidence-artifact media type mismatch")
    try:
        content = base64.b64decode(item["canonical_content_base64"], validate=True)
    except (ValueError, TypeError):
        raise ReleaseIdentityError("evidence-artifact base64 is invalid")
    if item.get("byte_length") != len(content) or item.get("content_sha256") != _sha256_bytes(
        content
    ):
        raise ReleaseIdentityError("evidence-artifact content digest mismatch")
    return content


def _decode_generator_receipt(wrapper: Mapping[str, Any], observation: Mapping[str, Any]) -> bytes:
    raw = _decode_artifact(wrapper, producer_id="PROD-CMD-PACKET:B11.1")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReleaseIdentityError("generator receipt is not canonical JSON")
    if (
        canonical_bytes(receipt) != raw
        or set(receipt) != set(GENERATOR_RECEIPT_FIELDS)
        or receipt.get("schema_version") != 1
        or receipt.get("artifact_kind") != "generator-run-receipt"
        or receipt.get("generator_id") != observation.get("generator_id")
    ):
        raise ReleaseIdentityError("generator-run-receipt-v1 schema mismatch")
    if (
        wrapper.get("detail_schema_id") != "generator-run-receipt-v1"
        or wrapper.get("artifact_id")
        != f"GENERATOR-RUN:{receipt['generator_id']}:{receipt['run_ordinal']}"
    ):
        raise ReleaseIdentityError("generator receipt artifact binding mismatch")
    argv = receipt.get("argv")
    if (
        not isinstance(argv, list)
        or receipt.get("argv_sha256") != framed_hash("astrid_runtime.generator-run-argv.v1", argv)
        or any(Path(str(x)).is_absolute() or ".." in Path(str(x)).parts for x in argv)
    ):
        raise ReleaseIdentityError("generator receipt argv is not portable")
    if not isinstance(receipt.get("output_rows"), list) or any(
        set(row) != {"path", "sha256", "byte_length"}
        or Path(str(row.get("path"))).is_absolute()
        or ".." in Path(str(row.get("path"))).parts
        or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256")))
        or not isinstance(row.get("byte_length"), int)
        or row.get("byte_length") < 0
        for row in receipt["output_rows"]
    ):
        raise ReleaseIdentityError("generator receipt output inventory schema mismatch")
    if receipt.get("exit_code") != 0:
        raise ReleaseIdentityError("generator receipt exit code is not successful")
    return raw


def _validate_git_identity_bytes(content: bytes, row: Mapping[str, Any]) -> None:
    try:
        identity = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReleaseIdentityError("Git identity evidence is not canonical JSON")
    if canonical_bytes(identity) != content or set(identity) != {
        "repository_identity",
        "head_oid",
        "head_ref",
        "detached",
        "git_dir_kind",
        "git_dir_relative",
        "common_dir_relative",
        "submodules",
    }:
        raise ReleaseIdentityError("Git identity evidence schema mismatch")
    if identity.get("repository_identity") != row.get("repository_identity") or identity.get(
        "head_oid"
    ) != row.get("integrated_oid"):
        raise ReleaseIdentityError("Git identity evidence is not bound to candidate row")
    if not isinstance(identity.get("submodules"), list):
        raise ReleaseIdentityError("Git submodule identity evidence is not a list")
    for sub in identity["submodules"]:
        if (
            set(sub)
            != {
                "path",
                "oid",
                "head_ref",
                "detached",
                "git_dir_relative",
                "common_dir_relative",
                "dirty_paths",
            }
            or not isinstance(sub.get("dirty_paths"), list)
            or Path(str(sub.get("path"))).is_absolute()
            or ".." in Path(str(sub.get("path"))).parts
        ):
            raise ReleaseIdentityError("Git submodule identity evidence schema mismatch")


def create_pre_live_identity(
    components: Mapping[str, str | os.PathLike[str]],
    *,
    metadata: Mapping[str, Any] | None = None,
    output: str | os.PathLike[str] | None = None,
    seed_outputs: Mapping[str, bytes | bytearray | Mapping[str, Any] | Sequence[Any]] | None = None,
    generator_definitions: Sequence[Mapping[str, Any]] | None = None,
    contract_bytes: bytes | None = None,
    schema_manifest_bytes: bytes | None = None,
    component_manifest_bytes: bytes | None = None,
    registry_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    _assert_clean(components, output)
    rows = resolve_reviewed_components(components)
    if generator_definitions is not None:
        if contract_bytes is None or schema_manifest_bytes is None:
            raise ReleaseIdentityError("B11.1 requires complete contract and schema-manifest bytes")
        rows = run_b11_1(
            rows,
            generator_definitions,
            contract_bytes=contract_bytes,
            schema_manifest_bytes=schema_manifest_bytes,
            component_manifest_bytes=component_manifest_bytes,
        )
    metadata = dict(metadata or {})
    planned = set(components) == {"ASTRID-CLIENT", "NEUTRAL-RUNTIME"}
    if seed_outputs is None:
        raise ReleaseIdentityError(
            f"PRELIVE-MANIFEST requires actual bytes for all {PRELIVE_SEED_COUNT} seeds"
        )
    definitions = sorted(
        (dict(d) for d in (generator_definitions or [])), key=lambda d: d.get("generator_id", "")
    )
    definition_digests = []
    if len({d.get("generator_id") for d in definitions}) != len(definitions) or any(
        not isinstance(d.get("generator_id"), str) for d in definitions
    ):
        raise ReleaseIdentityError("generator definitions must have unique IDs")
    if definitions:
        epochs = dict(metadata.get("epochs", {}))
        epochs["generator_definition_digests"] = [
            _sha256_bytes(canonical_bytes(d)) for d in definitions
        ]
        metadata["epochs"] = epochs
    manifest = build_prelive_manifest(seed_outputs, metadata=metadata)
    seed_wrappers = _seed_payload_wrappers(seed_outputs, metadata=metadata)
    evidence = []
    for row in rows:
        data = canonical_bytes(row)
        digest = _sha256_bytes(data)
        evidence.append(
            {
                "path": f"evidence/sha256/{digest[:2]}/{digest}",
                "sha256": digest,
                "producer_id": "CMD-IDENTITY:pre-live-root",
                "token_ids": [row["component_id"]],
                "epochs": metadata.get("epochs", {}),
                "media_type": "application/json",
            }
        )
    evidence.sort(key=lambda r: (r["path"], r["sha256"], r["producer_id"]))
    root_payload = {
        "component_rows": rows,
        "evidence_rows": evidence,
        "manifest_sha256": manifest["manifest_sha256"],
    }
    identity = framed_hash("astrid_runtime.pre-live-evidence-root.v1", root_payload)
    strict = set(r["component_id"] for r in rows) == {"ASTRID-CLIENT", "NEUTRAL-RUNTIME"}
    locators = join_plan_remote_targets(rows, registry_rows=registry_rows) if strict else []
    identity_evidence = []
    for row in rows:
        binding = next(
            (
                b
                for b in row["provenance_input_bindings"]
                if b.get("input_id") == f"GIT-IDENTITY:{row['component_id']}"
            ),
            None,
        )
        if binding is None:
            raise ReleaseIdentityError(
                "candidate row lacks schema-legal Git identity evidence binding"
            )
        identity_evidence.append(
            _artifact_wrapper(
                binding["input_id"],
                "CMD-IDENTITY:pre-live-root",
                canonical_bytes(git_identity(Path(components[row["component_id"]]))),
            )
        )
    generator_definition_evidence = []
    for definition in definitions:
        gid = definition.get("generator_id")
        if isinstance(gid, str):
            generator_definition_evidence.append(
                _artifact_wrapper(
                    f"GENERATOR-DEFINITION:{gid}",
                    "CMD-IDENTITY:pre-live-root",
                    canonical_bytes(definition),
                    detail_schema_id="generator-definition-v1",
                )
            )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": "pre-live-root",
        "operation_id": "CMD-IDENTITY:pre-live-root",
        "identity": identity,
        "pre_live_manifest": manifest,
        "pre_live_seed_payloads": seed_wrappers,
        "component_identity_evidence": identity_evidence,
        "generator_definition_evidence": generator_definition_evidence,
        "evidence_rows": evidence,
        "component_rows": rows,
        "plan_registry_enforced": bool(locators),
        "remote_target_locators": locators,
        "remote_target_registry_sha256": component_registry_sha256(plan_component_registry())
        if locators
        else NONE,
        "metadata": _nfc(metadata),
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    _write_receipt(receipt, output)
    return receipt


def _component_set(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result = {}
    for row in rows:
        if set(row) != set(CANDIDATE_COMPONENT_FIELDS):
            raise ReleaseIdentityError(
                "candidate-component-row-v1 has unexpected or missing fields"
            )
        cid = row["component_id"]
        if not isinstance(cid, str) or cid in result:
            raise ReleaseIdentityError("candidate component IDs must be unique")
        result[cid] = row
    return result


def create_candidate_core_identity(
    pre_live: Mapping[str, Any] | str | os.PathLike[str],
    components: Mapping[str, str | os.PathLike[str]],
    *,
    metadata: Mapping[str, Any] | None = None,
    output: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    source = load_receipt(pre_live) if isinstance(pre_live, (str, os.PathLike)) else dict(pre_live)
    verify_receipt(source)
    if source.get("kind") != "pre-live-root":
        raise ReleaseIdentityError("candidate core requires a pre-live-root receipt")
    _assert_clean(components, output)
    recomputed_rows = resolve_reviewed_components(components)
    old, new = _component_set(source.get("component_rows", [])), _component_set(recomputed_rows)
    if set(old) != set(new):
        raise ReleaseIdentityError("candidate component set is not a total bijection")
    for cid in sorted(old):
        for field in CANDIDATE_COMPONENT_FIELDS:
            if field in {"generator_ids", "generator_observation_rows"}:
                continue
            if canonical_bytes(old[cid][field]) != canonical_bytes(new[cid][field]):
                raise ReleaseIdentityError(
                    f"candidate component field mismatch after pre-live capture: {cid}:{field}"
                )
    rows = [copy.deepcopy(old[cid]) for cid in sorted(old)]
    meta = dict(metadata or {})
    manifest = source["pre_live_manifest"]
    core = {
        "schema_version": 1,
        "governance_binding": meta.get("governance_binding", "LOCAL-STAGE1-RELEASE"),
        "component_manifest_sha256": framed_hash("astrid_runtime.component-manifest.v1", rows),
        "contract_id": meta.get(
            "contract_id",
            framed_hash("astrid_runtime.contract.v1", [r["contract_sha256"] for r in rows]),
        ),
        "runtime_build_id": meta.get(
            "runtime_build_id",
            framed_hash("astrid_runtime.runtime-build.v1", [r["integrated_oid"] for r in rows]),
        ),
        "source_manifest_id": meta.get(
            "source_manifest_id",
            framed_hash("astrid_runtime.source-manifest.v1", [r["subtree_sha256"] for r in rows]),
        ),
        "migration_manifest_id": meta.get("migration_manifest_id", NONE),
        "selected_realm_id": meta.get("selected_realm_id", NONE),
        "trusted_disposition_sha256": meta.get("trusted_disposition_sha256", NONE),
        "pre_live_evidence_root": source["identity"],
        "contract_epoch": meta.get("contract_epoch", NONE),
        "runtime_epoch": meta.get("runtime_epoch", NONE),
        "source_epoch": meta.get("source_epoch", NONE),
        "migration_epoch": meta.get("migration_epoch", NONE),
        "activation_epoch": meta.get("activation_epoch", NONE),
        "release_epoch": NONE,
        "component_rows": rows,
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": "candidate-core",
        "operation_id": "CMD-IDENTITY:candidate-core",
        "identity": framed_hash("astrid_runtime.candidate-core.v1", core),
        "candidate_core": core,
        "pre_live_root": source["identity"],
        "pre_live_component_rows": copy.deepcopy(source["component_rows"]),
        "pre_live_evidence_rows": copy.deepcopy(source["evidence_rows"]),
        "pre_live_manifest": copy.deepcopy(manifest),
        "pre_live_manifest_sha256": manifest["manifest_sha256"],
        "metadata": _nfc(meta),
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    _write_receipt(receipt, output)
    return receipt


def _configured_receipt_root() -> Path | None:
    for name in RECEIPT_ROOT_ENVIRONMENTS:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return None


def _safe_receipt_path(path: str | os.PathLike[str], root: Path | None = None) -> Path:
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise ReleaseIdentityError("receipt path may not contain '..'")
    target_raw = raw.absolute()
    target = target_raw.resolve(strict=False)
    current = Path(target_raw.anchor)
    for part in target.parts[1:-1]:
        current /= part
        if (
            current.exists()
            and current.is_symlink()
            and current not in {Path("/tmp"), Path("/var")}
        ):
            raise ReleaseIdentityError("receipt path contains a symlink")
    if target_raw.exists() and target_raw.is_symlink():
        raise ReleaseIdentityError("receipt path is a symlink")
    if root is not None:
        try:
            target.relative_to(root)
        except ValueError:
            raise ReleaseIdentityError("receipt path is outside configured receipt root")
    return target


def _write_receipt(receipt: Mapping[str, Any], output: str | os.PathLike[str] | None) -> None:
    if output is None:
        return
    target = _safe_receipt_path(output, _configured_receipt_root())
    target.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(receipt) + b"\n"
    target.write_bytes(data)
    if target.read_bytes() != data:
        raise ReleaseIdentityError("stored receipt bytes changed during write")


def verify_receipt(receipt: Mapping[str, Any]) -> str:
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get(
        "receipt_sha256"
    ) != _receipt_digest(receipt):
        raise ReleaseIdentityError("release receipt digest or schema mismatch")
    if receipt.get("kind") == "pre-live-root":
        manifest = receipt.get("pre_live_manifest")
        if (
            not isinstance(manifest, Mapping)
            or set(manifest) != set(PRELIVE_MANIFEST_FIELDS)
            or manifest.get("schema_version") != PRELIVE_MANIFEST_SCHEMA
            or manifest.get("seed_ids") != list(PRELIVE_SEEDS)
            or len(manifest.get("seed_ids", [])) != PRELIVE_SEED_COUNT
        ):
            raise ReleaseIdentityError("PRELIVE-MANIFEST seed/schema projection mismatch")
        evidence = manifest.get("evidence_rows")
        if not isinstance(evidence, list) or len(evidence) != PRELIVE_SEED_COUNT:
            raise ReleaseIdentityError("PRELIVE-MANIFEST evidence cardinality mismatch")
        for row in evidence:
            if (
                set(row) != {"path", "sha256", "producer_id", "token_ids", "epochs", "media_type"}
                or not isinstance(row.get("token_ids"), list)
                or len(row["token_ids"]) != 1
                or row["token_ids"][0] not in PRELIVE_SEEDS
                or row.get("path")
                != f"evidence/sha256/{row.get('sha256', '')[:2]}/{row.get('sha256', '')}"
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256")))
                or not isinstance(row.get("producer_id"), str)
                or not isinstance(row.get("media_type"), str)
            ):
                raise ReleaseIdentityError("PRELIVE-MANIFEST evidence row mismatch")
        if {row["token_ids"][0] for row in evidence} != set(PRELIVE_SEEDS):
            raise ReleaseIdentityError("PRELIVE-MANIFEST evidence is not a bijection")
        payloads = receipt.get("pre_live_seed_payloads")
        if (
            not isinstance(payloads, list)
            or len(payloads) != PRELIVE_SEED_COUNT
            or {
                p.get("artifact_id", "").removeprefix("PRELIVE-SEED:")
                for p in payloads
                if isinstance(p, Mapping)
            }
            != set(PRELIVE_SEEDS)
        ):
            raise ReleaseIdentityError("PRELIVE seed payload wrappers are incomplete")
        for payload in payloads:
            seed = payload.get("artifact_id", "").removeprefix("PRELIVE-SEED:")
            matching = next((row for row in evidence if row["token_ids"] == [seed]), None)
            if matching is None:
                raise ReleaseIdentityError("PRELIVE seed wrapper is not bound to manifest evidence")
            content = _decode_artifact(
                payload,
                artifact_id=f"PRELIVE-SEED:{seed}",
                producer_id=matching["producer_id"],
                media_type=matching["media_type"],
            )
            if matching["sha256"] != payload["content_sha256"]:
                raise ReleaseIdentityError("PRELIVE seed wrapper is not bound to manifest evidence")
        identity_evidence = receipt.get("component_identity_evidence")
        rows_for_identity = receipt.get("component_rows", [])
        if not isinstance(identity_evidence, list) or len(identity_evidence) != len(
            rows_for_identity
        ):
            raise ReleaseIdentityError("component identity evidence is incomplete")
        for row in rows_for_identity:
            binding = next(
                (
                    b
                    for b in row.get("provenance_input_bindings", [])
                    if b.get("input_id") == f"GIT-IDENTITY:{row.get('component_id')}"
                ),
                None,
            )
            evidence_item = next(
                (
                    item
                    for item in identity_evidence
                    if item.get("artifact_id") == (binding or {}).get("input_id")
                ),
                None,
            )
            if (
                binding is None
                or evidence_item is None
                or evidence_item.get("content_sha256") != binding.get("sha256")
            ):
                raise ReleaseIdentityError("component Git identity is not bound by candidate row")
            identity_content = _decode_artifact(
                evidence_item,
                artifact_id=binding["input_id"],
                producer_id="CMD-IDENTITY:pre-live-root",
            )
            if _sha256_bytes(identity_content) != binding["sha256"]:
                raise ReleaseIdentityError("component identity evidence digest mismatch")
            _validate_git_identity_bytes(identity_content, row)
        definition_evidence = receipt.get("generator_definition_evidence", [])
        if not isinstance(definition_evidence, list):
            raise ReleaseIdentityError("generator definition evidence is not a list")
        observations = [
            observation
            for row in rows_for_identity
            for observation in row.get("generator_observation_rows", [])
        ]
        if len(definition_evidence) != len(observations) or [
            item.get("artifact_id", "").removeprefix("GENERATOR-DEFINITION:")
            for item in definition_evidence
        ] != [
            observation.get("generator_id")
            for observation in sorted(observations, key=lambda item: item.get("generator_id", ""))
        ]:
            raise ReleaseIdentityError(
                "generator definition evidence cardinality or order mismatch"
            )
        definition_digests = []
        for item, observation in zip(
            definition_evidence, sorted(observations, key=lambda item: item.get("generator_id", ""))
        ):
            raw = _decode_artifact(
                item,
                artifact_id=f"GENERATOR-DEFINITION:{observation['generator_id']}",
                producer_id="CMD-IDENTITY:pre-live-root",
            )
            if item.get("detail_schema_id") != "generator-definition-v1" or _sha256_bytes(
                raw
            ) != observation.get("generator_definition_sha256"):
                raise ReleaseIdentityError("generator definition evidence digest mismatch")
            try:
                definition = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ReleaseIdentityError("generator definition evidence is not canonical JSON")
            if canonical_bytes(definition) != raw:
                raise ReleaseIdentityError("generator definition evidence bytes are not canonical")
            definition_digests.append(_sha256_bytes(raw))
        if manifest.get("epochs", {}).get("generator_definition_digests", []) != definition_digests:
            raise ReleaseIdentityError("PRELIVE manifest is not bound to generator definitions")
        for row in rows_for_identity:
            for observation in row.get("generator_observation_rows", []):
                if set(observation) != set(GENERATOR_ROW_FIELDS):
                    raise ReleaseIdentityError("generator observation schema mismatch")
                wrappers = observation.get("run_receipt_evidence_rows")
                if not isinstance(wrappers, list) or len(wrappers) != 2:
                    raise ReleaseIdentityError("generator receipt evidence is incomplete")
                for index, wrapper in enumerate(wrappers):
                    raw = _decode_generator_receipt(wrapper, observation)
                    expected_sha = (
                        observation["first_run_receipt_sha256"]
                        if index == 0
                        else observation["second_run_receipt_sha256"]
                    )
                    if _sha256_bytes(raw) != expected_sha:
                        raise ReleaseIdentityError(
                            "generator receipt wrapper is not bound to its observation row"
                        )
        if manifest.get("manifest_sha256") != framed_hash(
            "astrid_runtime.pre-live-manifest.v1",
            {k: manifest[k] for k in manifest if k != "manifest_sha256"},
        ):
            raise ReleaseIdentityError("pre-live manifest digest mismatch")
        rows = _component_set(receipt.get("component_rows", []))
        expected = framed_hash(
            "astrid_runtime.pre-live-evidence-root.v1",
            {
                "component_rows": [rows[k] for k in sorted(rows)],
                "evidence_rows": receipt.get("evidence_rows"),
                "manifest_sha256": manifest["manifest_sha256"],
            },
        )
        planned_ids = {"ASTRID-CLIENT", "NEUTRAL-RUNTIME"}
        planned = {row.get("component_id") for row in receipt.get("component_rows", [])} == {
            "ASTRID-CLIENT",
            "NEUTRAL-RUNTIME",
        }
        if planned and not receipt.get("plan_registry_enforced"):
            raise ReleaseIdentityError("planned pre-live receipt lacks registry enforcement")
        if receipt.get("plan_registry_enforced"):
            locators = receipt.get("remote_target_locators")
            if (
                not isinstance(locators, list)
                or len(locators) != 3
                or receipt.get("remote_target_registry_sha256")
                != component_registry_sha256(plan_component_registry())
            ):
                raise ReleaseIdentityError("planned pre-live receipt lacks exact remote locators")
            if locators[-1] != plan_publication_row():
                raise ReleaseIdentityError("publication locator is not the plan-owned row")
            if locators[:2] != join_plan_remote_targets(receipt.get("component_rows", []))[:2]:
                raise ReleaseIdentityError("planned locator order or join is not exact")
    elif receipt.get("kind") == "candidate-core":
        core = receipt.get("candidate_core")
        if not isinstance(core, Mapping) or set(core) != set(CANDIDATE_CORE_FIELDS):
            raise ReleaseIdentityError("candidate-core-object-v1 has unexpected or missing fields")
        if receipt.get("pre_live_root") != core.get("pre_live_evidence_root"):
            raise ReleaseIdentityError(
                "candidate pre-live root does not match candidate core evidence root"
            )
        manifest = receipt.get("pre_live_manifest")
        if (
            not isinstance(manifest, Mapping)
            or set(manifest) != set(PRELIVE_MANIFEST_FIELDS)
            or manifest.get("manifest_sha256") != receipt.get("pre_live_manifest_sha256")
            or manifest.get("manifest_sha256")
            != framed_hash(
                "astrid_runtime.pre-live-manifest.v1",
                {k: manifest[k] for k in manifest if k != "manifest_sha256"},
            )
        ):
            raise ReleaseIdentityError(
                "candidate core is not bound to the verified PRELIVE manifest"
            )
        prior_rows = _component_set(receipt.get("pre_live_component_rows", []))
        core_rows = _component_set(core.get("component_rows", []))
        if set(prior_rows) != set(core_rows) or any(
            canonical_bytes(prior_rows[c]) != canonical_bytes(core_rows[c]) for c in prior_rows
        ):
            raise ReleaseIdentityError(
                "candidate core component set is not a total field-equal bijection"
            )
        if core.get("component_manifest_sha256") != framed_hash(
            "astrid_runtime.component-manifest.v1", core["component_rows"]
        ):
            raise ReleaseIdentityError("candidate core component manifest binding mismatch")
        reconstructed_root = framed_hash(
            "astrid_runtime.pre-live-evidence-root.v1",
            {
                "component_rows": [prior_rows[c] for c in sorted(prior_rows)],
                "evidence_rows": receipt.get("pre_live_evidence_rows"),
                "manifest_sha256": manifest["manifest_sha256"],
            },
        )
        if reconstructed_root != receipt.get("pre_live_root"):
            raise ReleaseIdentityError("candidate pre-live root is not a verified PRELIVE root")
        expected = framed_hash("astrid_runtime.candidate-core.v1", core)
    else:
        raise ReleaseIdentityError("unknown release receipt kind")
    if expected != receipt.get("identity"):
        raise ReleaseIdentityError("release identity mismatch")
    return expected


def load_receipt(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = _safe_receipt_path(path, _configured_receipt_root())
    try:
        raw = target.read_bytes()
        if not raw.endswith(b"\n"):
            raise ReleaseIdentityError("receipt is not canonical stored bytes")
        value = json.loads(raw[:-1].decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseIdentityError(f"cannot retrieve release receipt: {target}") from exc
    if not isinstance(value, dict) or canonical_bytes(value) + b"\n" != raw:
        raise ReleaseIdentityError("receipt bytes are not canonical")
    verify_receipt(value)
    return value


def bind_remote_targets(
    receipt: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    verify_receipt(receipt)
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise ReleaseIdentityError("remote targets must be an array")
    if not targets:
        raise ReleaseIdentityError("remote targets must be non-empty")
    result = copy.deepcopy(dict(receipt))
    rows = []
    seen: set[str] = set()
    if result.get("plan_registry_enforced") and (
        not isinstance(result.get("remote_target_locators"), list)
        or not result["remote_target_locators"]
    ):
        raise ReleaseIdentityError("planned receipt requires non-empty remote locators")
    if result.get("remote_target_locators") and list(targets) != result["remote_target_locators"]:
        raise ReleaseIdentityError("remote target rows are not the plan-owned locator join")
    for target in targets:
        # Legacy callers may bind an unplanned, copy-only annotation.  Once a
        # plan-owned locator is present the closed row schema is mandatory.
        if not result.get("remote_target_locators"):
            if {r.get("component_id") for r in result.get("component_rows", [])} == {
                "ASTRID-CLIENT",
                "NEUTRAL-RUNTIME",
            }:
                raise ReleaseIdentityError("planned component receipt requires remote locators")
            item = _nfc(dict(target))
            tid = item.get("remote_target_id")
            if not isinstance(tid, str) or not tid or tid in seen:
                raise ReleaseIdentityError("remote target ids must be unique non-empty strings")
            seen.add(tid)
            if set(item) != set(REMOTE_TARGET_FIELDS):
                raise ReleaseIdentityError("remote-target-row-v1 has unexpected or missing fields")
            _validate_url(item["canonical_url"])
            rows.append(item)
            continue
        if set(target) != set(REMOTE_TARGET_FIELDS):
            raise ReleaseIdentityError("remote-target-row-v1 has unexpected fields")
        item = _nfc(dict(target))
        tid = item["remote_target_id"]
        if not isinstance(tid, str) or not tid or tid in seen:
            raise ReleaseIdentityError("remote target ids must be unique non-empty strings")
        seen.add(tid)
        if item["target_kind"] in {"component", "publication"}:
            _validate_url(item["canonical_url"])
        rows.append(item)
    result["remote_targets"] = (
        rows
        if result.get("remote_target_locators")
        else sorted(rows, key=lambda r: r["remote_target_id"])
    )
    result["remote_target_registry_sha256"] = result.get("remote_target_registry_sha256") or (
        component_registry_sha256(plan_component_registry())
        if result.get("remote_target_locators")
        else component_registry_sha256(rows)
    )
    result["receipt_sha256"] = _receipt_digest(result)
    return result


def _component_args(values: Sequence[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ReleaseIdentityError("--component values must use COMPONENT_ID=CHECKOUT")
        cid, checkout = value.split("=", 1)
        if not cid or not checkout or cid in result:
            raise ReleaseIdentityError("--component values must use unique COMPONENT_ID=CHECKOUT")
        result[cid] = checkout
    if not result:
        raise ReleaseIdentityError("at least one --component is required")
    return result


def _load_seed_inputs(
    seed_dir: str | os.PathLike[str], manifest_path: str | os.PathLike[str]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    root = Path(seed_dir).expanduser().resolve()
    if not root.is_dir():
        raise ReleaseIdentityError("--seed-dir must be an existing directory")
    try:
        descriptor = json.loads(Path(manifest_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseIdentityError("--seed-manifest must be readable canonical JSON") from exc
    entries = (
        descriptor.get("seeds")
        if isinstance(descriptor, Mapping) and "seeds" in descriptor
        else descriptor
    )
    if isinstance(entries, list):
        entries = {item.get("seed_id"): item for item in entries if isinstance(item, Mapping)}
    if not isinstance(entries, Mapping) or set(entries) != set(PRELIVE_SEEDS):
        raise ReleaseIdentityError(
            f"seed manifest must enumerate exactly all {PRELIVE_SEED_COUNT} seed IDs"
        )
    outputs: dict[str, bytes] = {}
    definitions: dict[str, Any] = {}
    for seed in PRELIVE_SEEDS:
        item = entries[seed]
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise ReleaseIdentityError(f"seed manifest entry is invalid: {seed}")
        rel = Path(item["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ReleaseIdentityError(f"seed path is not relative and contained: {seed}")
        raw_target = root / rel
        if raw_target.is_symlink():
            raise ReleaseIdentityError(f"seed payload path may not be a symlink: {seed}")
        target = raw_target.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ReleaseIdentityError(f"seed path escapes --seed-dir: {seed}") from exc
        if not target.is_file() or target.is_symlink():
            raise ReleaseIdentityError(f"seed payload is missing or not a regular file: {seed}")
        outputs[seed] = target.read_bytes()
        producer, media = (
            item.get("producer_id", "CMD-PRELIVE-MANIFEST"),
            item.get("media_type", "application/json"),
        )
        if not isinstance(producer, str) or not isinstance(media, str) or not producer or not media:
            raise ReleaseIdentityError(f"seed media/producer definition is invalid: {seed}")
        definitions[seed] = {"producer_id": producer, "media_type": media}
    return outputs, {"seed_artifacts": definitions}


def _load_json_input(path: str | os.PathLike[str], label: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseIdentityError(f"{label} must be readable JSON") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astrid-release-identity")
    sub = parser.add_subparsers(dest="operation", required=True)
    pre = sub.add_parser("pre-live")
    pre.add_argument("--component", action="append", default=[])
    pre.add_argument("--output")
    pre.add_argument("--seed-dir", required=True)
    pre.add_argument("--seed-manifest", required=True)
    pre.add_argument("--generator-manifest")
    pre.add_argument("--registry")
    pre.add_argument("--contract-file")
    pre.add_argument("--schema-manifest-file")
    pre.add_argument("--component-manifest-file")
    candidate = sub.add_parser("candidate-core")
    candidate.add_argument("--pre-live", required=True)
    candidate.add_argument("--component", action="append", default=[])
    candidate.add_argument("--output")
    verify = sub.add_parser("verify")
    verify.add_argument("receipt")
    args = parser.parse_args(argv)
    try:
        if args.operation == "pre-live":
            seed_outputs, seed_metadata = _load_seed_inputs(args.seed_dir, args.seed_manifest)
            definitions = (
                _load_json_input(args.generator_manifest, "--generator-manifest")
                if args.generator_manifest
                else None
            )
            definitions = (
                definitions.get("generators", definitions)
                if isinstance(definitions, Mapping)
                else definitions
            )
            registry = _load_json_input(args.registry, "--registry") if args.registry else None
            contract = Path(args.contract_file).read_bytes() if args.contract_file else None
            schema = (
                Path(args.schema_manifest_file).read_bytes() if args.schema_manifest_file else None
            )
            component = (
                Path(args.component_manifest_file).read_bytes()
                if args.component_manifest_file
                else None
            )
            result = create_pre_live_identity(
                _component_args(args.component),
                output=args.output,
                seed_outputs=seed_outputs,
                metadata=seed_metadata,
                generator_definitions=definitions,
                contract_bytes=contract,
                schema_manifest_bytes=schema,
                component_manifest_bytes=component,
                registry_rows=registry,
            )
        elif args.operation == "candidate-core":
            result = create_candidate_core_identity(
                args.pre_live, _component_args(args.component), output=args.output
            )
        else:
            result = {"ok": True, "identity": load_receipt(args.receipt)["identity"]}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except ReleaseIdentityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
