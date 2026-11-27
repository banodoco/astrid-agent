#!/usr/bin/env python3
"""Materialize the witnessed motion source profile, never install it."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WITNESS_PATH = ROOT / "astrid/resources/astrid_motion_projection.json"
RELEASE_SOURCE_PATH = ROOT / "astrid/resources/astrid_motion_release_source.json"
PROFILES = ("legacy-projection", "direct-source")
# identity.py is stdlib-only; loading the leaf avoids requiring the full Agent
# environment merely to generate browser vendor inputs.
_spec = importlib.util.spec_from_file_location(
    "_astrid_projection_identity", ROOT / "astrid/core/pack/identity.py"
)
assert _spec is not None and _spec.loader is not None
_identity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_identity)
pack_digest = _identity.pack_digest
_source_spec = importlib.util.spec_from_file_location(
    "_astrid_motion_source", ROOT / "astrid/core/pack/motion_source.py"
)
assert _source_spec is not None and _source_spec.loader is not None
_motion_source = importlib.util.module_from_spec(_source_spec)
_source_spec.loader.exec_module(_motion_source)

ALIASES = {
    "rendering/elements/_shared/am.tsx": b"../../../../rendering/shared/element_contracts",
    "rendering/elements/effects/am-discord/component.tsx": b"../../../../../rendering/shared/element_contracts",
    "rendering/elements/effects/am-presenter/component.tsx": b"../../../../../rendering/shared/element_contracts",
    "rendering/elements/effects/am-presenter/scene.ts": b"../../../../../rendering/shared/element_contracts",
}
ALIAS = b"@astrid-rendering/shared/element_contracts"
MANIFEST_MARKER = b'  "id": "astrid_motion",\n'
MANIFEST_INSERT = (
    b'  "pack_api": "2.0",\n'
    b'  "dependencies": {"python_requirements": ["Pillow"]},\n'
)


def witness() -> dict:
    return json.loads(WITNESS_PATH.read_text(encoding="utf-8"))


def profile_witness(profile: str = "legacy-projection") -> dict:
    if profile == "legacy-projection":
        return witness()
    if profile == "direct-source":
        return _motion_source.read_release_source(RELEASE_SOURCE_PATH)
    raise ValueError(f"Unknown motion source profile: {profile}")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE
    )


def _check_digest(root: Path, expected: str, stage: str) -> None:
    actual = pack_digest(root)
    if actual != expected:
        raise ValueError(f"{stage} digest mismatch: expected {expected}, got {actual}")


def check_profile(root: Path, record: dict, profile: str) -> None:
    _check_digest(root, record["tree_digest"], "projection")
    if profile == "direct-source":
        count = _motion_source.source_file_count(root, _identity._EXCLUDED_DIRS)
        if count != record["file_count"]:
            raise ValueError(f"Unexpected direct source file count {count}")


def _source(repo: Path, destination: Path, record: dict) -> None:
    revision = record["upstream_commit"]
    treeish = f"{revision}:{record['upstream_subpath']}"
    if _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip().decode() != revision:
        raise ValueError("Unexpected upstream commit")
    if _git(repo, "rev-parse", "--verify", treeish).strip().decode() != record["upstream_tree"]:
        raise ValueError("Unexpected upstream tree")
    entries = _git(repo, "ls-tree", "-r", "-z", treeish).split(b"\0")
    count = 0
    for entry in entries:
        if not entry:
            continue
        info, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = info.split(b" ")
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError("Projection source must contain only regular Git files")
        relative = raw_path.decode("utf-8")
        _identity._entry_path(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(_git(repo, "cat-file", "blob", blob.decode("ascii")))
        count += 1
    if count != record["file_count"]:
        raise ValueError(f"Unexpected upstream file count {count}")
    _check_digest(destination, record["upstream_digest"], "upstream")


def _aliases(root: Path) -> None:
    for relative, old in ALIASES.items():
        path = root / relative
        raw = path.read_bytes()
        if raw.count(old) != 1 or ALIAS in raw:
            raise ValueError(f"Expected exactly one original import in {relative}")
        path.write_bytes(raw.replace(old, ALIAS, 1))


def _manifest(root: Path) -> None:
    path = root / "pack.yaml"
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        value.get("id") != "astrid_motion"
        or "pack_api" in value
        or "dependencies" in value
        or raw.count(MANIFEST_MARKER) != 1
    ):
        raise ValueError("Unexpected original motion manifest")
    projected = raw.replace(MANIFEST_MARKER, MANIFEST_INSERT + MANIFEST_MARKER, 1)
    if json.loads(projected) != {
        **value,
        "pack_api": "2.0",
        "dependencies": {"python_requirements": ["Pillow"]},
    }:
        raise ValueError("Motion manifest projection changed unrelated values")
    path.write_bytes(projected)


def project(repo: Path, output: Path, *, profile: str = "legacy-projection") -> dict:
    """Read the immutable Git tree and publish only verified projected bytes."""
    repo = repo.resolve(strict=True)
    output = output.absolute()
    if output.name != "astrid_motion":
        raise ValueError("Output must be a new directory named astrid_motion")
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    if output.is_relative_to(repo) or output.is_relative_to(ROOT):
        raise ValueError("Projection output must be outside both Agent checkouts")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    record = profile_witness(profile)
    with tempfile.TemporaryDirectory(prefix=".motion-projection-", dir=parent) as temporary:
        staged = Path(temporary) / "astrid_motion"
        staged.mkdir()
        _source(repo, staged, record)
        if profile == "legacy-projection":
            _aliases(staged)
            _check_digest(staged, record["alias_only_digest"], "alias-only")
            _manifest(staged)
        check_profile(staged, record, profile)
        # copytree refuses an existing output; never replace a pack or collection.
        shutil.copytree(staged, output)
    check_profile(output, record, profile)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="legacy-projection")
    parser.add_argument("--upstream-repo", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Verify an existing projection; do not write")
    args = parser.parse_args()
    if args.check:
        if args.upstream_repo is not None:
            parser.error("--check does not take --upstream-repo")
        record = profile_witness(args.profile)
        check_profile(args.output, record, args.profile)
    else:
        if args.upstream_repo is None:
            parser.error("--upstream-repo is required when generating")
        record = project(args.upstream_repo, args.output, profile=args.profile)
    # Witness goes to stdout, outside the hashed pack; never write a synthetic PIN.
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
