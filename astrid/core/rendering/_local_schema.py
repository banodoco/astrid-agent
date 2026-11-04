"""Bind render validation to the selected Agent's Runtime-pinned contract."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from astrid.core.execution.platform_helpers import owned_by_current_user

SCHEMA_RELATIVE_PATH = Path("astrid/core/timeline/schema/timeline-config.schema.json")
READER_INPUTS = (
    Path("astrid/__init__.py"),
    Path("astrid/core/__init__.py"),
    Path("astrid/core/timeline/banodoco_schema.py"),
    Path("astrid/core/rendering/_local_schema.py"),
    Path("astrid/core/timeline/__init__.py"),
    Path("astrid/core/rendering/__init__.py"),
)


def verified_schema_path(source_root: Path, configured: str) -> Path:
    root = source_root.absolute()
    expected = root / SCHEMA_RELATIVE_PATH
    if configured != str(expected):
        raise ValueError("local timeline schema origin differs from selected Agent source")
    for leaf in (expected, root / "runtime.lock.json", *(root / p for p in READER_INPUTS)):
        if leaf.resolve(strict=True) != leaf:
            raise ValueError("local timeline schema source is aliased")
        for path in (leaf, *leaf.parents):
            if path.is_symlink():
                raise ValueError("local timeline schema source is aliased")
            facts = path.stat()
            if not owned_by_current_user(path, facts, forbidden=0o022):
                raise ValueError(f"local timeline schema input has a foreign owner/mode: {path.name}")
            if path == root:
                break
        facts = leaf.stat()
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"local timeline schema input is not a regular file: {leaf.name}")
    lock = json.loads((root / "runtime.lock.json").read_text(encoding="utf-8"))
    files = lock.get("files") if isinstance(lock, dict) else None
    pin = files.get(SCHEMA_RELATIVE_PATH.as_posix()) if isinstance(files, dict) else None
    if (not isinstance(pin, dict)
            or pin.get("from") != "contract/schemas/timeline-config.schema.json"):
        raise ValueError("local timeline schema has no approved Runtime source pin")
    actual = "sha256:" + hashlib.sha256(expected.read_bytes()).hexdigest()
    if pin.get("sha256") != actual:
        raise ValueError("local timeline schema does not match its Runtime source pin")
    return expected


def load_verified_schema(source_root: Path, configured: str) -> dict:
    """Use the canonical reader without accepting an ambient or cached contract."""
    path = verified_schema_path(source_root, configured)
    from astrid.core.timeline import banodoco_schema

    if (Path(banodoco_schema.__file__).absolute()
            != source_root / "astrid/core/timeline/banodoco_schema.py"
            or banodoco_schema._VENDORED_SCHEMA != path):
        raise ValueError("canonical timeline schema reader has a foreign origin")
    schema = banodoco_schema.load_vendored_timeline_schema()
    raw = path.read_bytes()
    if schema != json.loads(raw):
        raise ValueError("canonical timeline schema reader returned a stale contract")
    verified_schema_path(source_root, configured)
    json.dumps(schema, allow_nan=False)

    # All references belong to this pinned document; never resolve a remote
    # resource, including in an otherwise unused definition.
    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    raise ValueError("local timeline schema has a foreign reference")
                target = schema
                for token in ref[2:].split("/"):
                    target = target[token.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)
    import jsonschema

    jsonschema.validators.validator_for(schema).check_schema(schema)
    return schema
