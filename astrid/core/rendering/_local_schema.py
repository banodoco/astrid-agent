"""Bind render validation to the selected Agent's Runtime-pinned contract."""

from __future__ import annotations

import json
from pathlib import Path

from astrid.core.timeline._local_schema import (
    READER_INPUTS as READER_INPUTS,
)
from astrid.core.timeline._local_schema import (
    SCHEMA_RELATIVE_PATH as SCHEMA_RELATIVE_PATH,
)
from astrid.core.timeline._local_schema import (
    verified_schema_path,
)


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
