"""LE-12 rule case: the real entry importer's collision refusal."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from astrid.core.generation.workflow_entry_setup import entry_exists_error


def collision_case():
    from vibecomfy.entries import import_as_entry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        existing = root / "example"
        existing.mkdir()
        (existing / "entry.json").write_text(json.dumps({
            "id": "example", "kind": "image", "modes": ["t2i"],
            "bundle": "bundle", "needs": [], "binds": {}, "defaults": {},
        }), encoding="utf-8")
        try:
            import_as_entry(root / "source.json", "example", root)
        except FileExistsError as exc:
            return entry_exists_error(exc, entry_id="example", retry_argv=[
                "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
                "--placement", "runpod", "--dry-run", "--json",
            ]).to_envelope()
    raise AssertionError("existing entry did not refuse implicit replacement")


def unavailable_schema_case():
    """Exercise the actual setup refusal at its bounded HTTP boundary."""
    from unittest.mock import patch
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.core.generation.workflow_entry_setup import _attached_entry_schema

    with patch.object(ComfyTargetClient, "request_json", side_effect=ValueError("object_info request failed")) as read:
        try:
            _attached_entry_schema("http://127.0.0.1:8188", "example")
        except GenerationReadinessError as exc:
            assert read.call_count == 1
            assert read.call_args.args == ("/object_info",)
            return exc.to_envelope()
    raise AssertionError("unavailable attached schema did not refuse")


CASES = [
    ("entry_already_exists", "entry_exists", collision_case),
    ("attached_schema_unavailable", "target_schema_unavailable", unavailable_schema_case),
]
