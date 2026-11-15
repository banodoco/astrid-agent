#!/usr/bin/env python3
"""Print or update the VibeComfy revision Astrid is pinned to.

The pin authority is the packaged VibeComfy engine recipe. The dependency
adapter, CI and this command all read its digest-verified source pin.

    python scripts/vibecomfy_pin.py            # print the pinned revision
    python scripts/vibecomfy_pin.py [revision] [--checkout PATH]
    python scripts/vibecomfy_pin.py --source comfyui [revision]

Pass --source comfyui to read or bump the ComfyUI source through the same
authority chain. Omitting --source preserves the VibeComfy interface.

A bump rewrites the selected source revision in every
package-owned pin manifest, then the dependent ``{"path", "sha256"}`` pin
digests and the recipe digests in the generation.engine contributions, and finally prints the revision
read back through the digest-verified recipe authority. VibeComfy bumps also
refresh each local route's model-catalog template hash from the requested Git
commit. Supply a checkout with ``--checkout`` or
``ASTRID_VIBECOMFY_CHECKOUT`` for that verification. The command does not update
the source node lock, requirements locks or test literals that name the old
revision; coordinate those by hand. Run the VibeComfy-related tests against a
checkout at the new revision afterwards and set the same sha in the hosted
``install-manifest.json`` (or drop its ``vibecomfy`` entry so the installer
uses this pin).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    # Keep the documented checkout command usable without an editable install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrid.core.generation.engine_recipe import engine_source_revision

ROOT = Path(__file__).resolve().parents[1] / "astrid"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(source: str = "vibecomfy") -> str:
    return engine_source_revision("comfyui", "windows-x86_64-cu130", source)


def _git(checkout: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args], check=False,
        stdin=subprocess.DEVNULL, capture_output=True,
        text=not binary, timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip() if not binary else "git command failed"
        raise SystemExit(f"VibeComfy git operation failed: {detail}")
    return result.stdout


def _catalog_routes(text: str) -> list[tuple[str, int, int]]:
    """Return each local ready-template ID and its exact hash value span."""
    lines = text.splitlines(keepends=True)
    rows: list[tuple[str, int, int]] = []
    offset = 0
    pending: tuple[str, int] | None = None
    template_count = hash_count = 0
    for line in lines:
        template = re.match(r"^(\s+)template:\s*([^#\s]+)\s*(?:#.*)?$", line.rstrip("\n"))
        digest = re.match(r'^(\s+)template_hash:\s*"?(sha256:[0-9a-f]{64})"?\s*(?:#.*)?$', line.rstrip("\n"))
        if template:
            if pending:
                raise SystemExit(f"model catalog template has no hash: {pending[0]}")
            pending = (template.group(2), len(template.group(1)))
            template_count += 1
        elif digest and pending and len(digest.group(1)) == pending[1]:
            start = offset + digest.start(2)
            rows.append((pending[0], start, start + len(digest.group(2))))
            pending = None
            hash_count += 1
        elif re.match(r"^\s+template_hash:", line):
            raise SystemExit("model catalog contains an unpaired or malformed template_hash")
        elif line.strip() and not line.lstrip().startswith("#"):
            # A different route field means an unpaired template is malformed.
            if pending and not digest:
                raise SystemExit(f"model catalog template has no adjacent hash: {pending[0]}")
        offset += len(line)
    if pending or template_count != hash_count:
        raise SystemExit("model catalog has an unpaired ready-template hash")
    return rows


def _target_template_hashes(checkout: Path, revision: str, template_ids: list[str]) -> dict[str, str]:
    """Resolve target-revision templates with VibeComfy's runtime registry."""
    # Build an isolated ready_templates tree from the requested commit. Runtime
    # discovery then applies the same path and ID rules as generation itself.
    archive = _git(checkout, "archive", "--format=tar", revision, "ready_templates", binary=True)
    assert isinstance(archive, bytes)
    with tempfile.TemporaryDirectory(prefix="astrid-vibecomfy-ready-") as raw_tmp:
        temp = Path(raw_tmp)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            for member in bundle.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise SystemExit("VibeComfy ready-template archive contains an unsafe path")
                if not (member.isfile() or member.isdir()):
                    raise SystemExit("VibeComfy ready-template archive contains a link or special file")
                bundle.extract(member, temp)
        ready_root = temp / "ready_templates"
        source_root = temp
        checkout_text = str(checkout)
        sys.path.insert(0, checkout_text)
        try:
            from vibecomfy.registry.ready import repo_ready_template_discovery, resolve_ready_template
            from unittest.mock import patch
            with patch("vibecomfy.registry.ready.repo_ready_template_root", return_value=ready_root):
                discovery = repo_ready_template_discovery(root=ready_root)
            result: dict[str, str] = {}
            for template_id in template_ids:
                try:
                    record = resolve_ready_template(template_id, discovery)
                    resolved = Path(record.path).resolve(strict=True)
                    resolved.relative_to(ready_root.resolve(strict=True))
                except Exception as exc:
                    raise SystemExit(f"cannot resolve target ready template {template_id!r}: {exc}") from exc
                if str(getattr(record, "template_id", "")) != template_id or getattr(record, "source_scope", None) != "repo":
                    raise SystemExit(f"target ready template is not a canonical repo template: {template_id}")
                relative = resolved.relative_to(source_root.resolve()).as_posix()
                payload = _git(checkout, "show", f"{revision}:{relative}", binary=True)
                assert isinstance(payload, bytes)
                result[template_id] = "sha256:" + hashlib.sha256(payload).hexdigest()
            return result
        finally:
            try:
                sys.path.remove(checkout_text)
            except ValueError:
                pass


def bump(revision: str, source_name: str = "vibecomfy", checkout: Path | str | None = None) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("expected a full 40-character lowercase commit sha")
    checkout_raw = checkout or os.environ.get("ASTRID_VIBECOMFY_CHECKOUT")
    template_hashes: dict[str, str] = {}
    model_catalog = ROOT / "core" / "model_catalog" / "models.yaml"
    if source_name == "vibecomfy":
        if not checkout_raw:
            raise SystemExit("a VibeComfy checkout is required; pass --checkout or set ASTRID_VIBECOMFY_CHECKOUT")
        checkout_path = Path(checkout_raw).expanduser()
        if not checkout_path.is_absolute() or not checkout_path.is_dir():
            raise SystemExit("VibeComfy checkout must be an existing absolute directory")
        checkout_path = checkout_path.resolve()
        _git(checkout_path, "cat-file", "-e", f"{revision}^{{commit}}")
        catalog_text = model_catalog.read_text(encoding="utf-8")
        routes = _catalog_routes(catalog_text)
        if not routes:
            raise SystemExit("model catalog has no local ready-template routes")
        template_hashes = _target_template_hashes(checkout_path, revision, list(dict.fromkeys(r[0] for r in routes)))
        if set(template_hashes) != {row[0] for row in routes}:
            raise SystemExit("one or more catalog ready-template routes could not be verified")
    # Prepare every affected byte sequence first. Any missing or invalid input
    # fails above, before a single packaged file is written.
    writes: dict[Path, bytes] = {}
    for path in (ROOT / "packs").glob("*/pins.json"):
        data = _load(path)
        changed = False
        for name, source in data.get("sources", {}).items():
            if name == source_name:
                source["revision"] = revision
                changed = True
        if changed:
            writes[path] = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    for path in (ROOT / "packs").glob("*/*.json"):
        data = json.loads(writes.get(path, path.read_bytes()))
        ref = data.get("pins") if isinstance(data, dict) else None
        if isinstance(ref, dict) and set(ref) == {"path", "sha256"}:
            pin_path = ROOT / ref["path"]
            pin_payload = writes.get(pin_path, pin_path.read_bytes())
            ref["sha256"] = hashlib.sha256(pin_payload).hexdigest()
            writes[path] = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    for manifest in (ROOT / "packs").glob("*/extensions/generation.engine/*.yaml"):
        text = writes.get(manifest, manifest.read_bytes()).decode("utf-8")
        for recipe in set(re.findall(r"^\s+path: (packs/\S+)$", text, re.M)):
            recipe_path = ROOT / recipe
            payload = writes.get(recipe_path, recipe_path.read_bytes())
            digest = hashlib.sha256(payload).hexdigest()
            text = re.sub(rf"(path: {re.escape(recipe)}\n\s+sha256: )\"?[0-9a-f]+\"?",
                          lambda m: f'{m.group(1)}"{digest}"', text)
        writes[manifest] = text.encode("utf-8")
    if template_hashes:
        text = writes.get(model_catalog, model_catalog.read_bytes()).decode("utf-8")
        for template_id, start, end in reversed(_catalog_routes(text)):
            text = text[:start] + template_hashes[template_id] + text[end:]
        writes[model_catalog] = text.encode("utf-8")
    for path, payload in writes.items():
        path.write_bytes(payload)


def main(argv: list[str]) -> int:
    source = "vibecomfy"
    checkout: str | None = None
    if "--checkout" in argv:
        index = argv.index("--checkout")
        if index + 1 >= len(argv):
            raise SystemExit("--checkout requires an absolute VibeComfy checkout path")
        checkout = argv[index + 1]
        del argv[index:index + 2]
    if argv[:1] == ["--source"]:
        if len(argv) < 2 or argv[1] not in {"vibecomfy", "comfyui"}:
            raise SystemExit("expected --source vibecomfy or --source comfyui")
        source, argv = argv[1], argv[2:]
    if len(argv) > 1:
        raise SystemExit(__doc__)
    if argv:
        bump(argv[0], source, checkout)
    print(read(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
