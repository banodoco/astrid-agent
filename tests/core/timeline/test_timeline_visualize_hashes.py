"""Fixture integrity test for the desert timeline_visualize slice.

Guards the frozen hashes in ``desert_truth.json`` against drift:

* ``assembly_sha256`` / ``registry_sha256`` must be the canonical-JSON
  SHA-256 (``json.dumps(obj, sort_keys=True, separators=(",", ":"),
  allow_nan=False)`` UTF-8 bytes) of the corresponding slice file.
* ``head_last_hash`` must equal the last event's ``hash`` in
  ``assembly.jsonl`` and ``assembly.head.json``'s ``last_hash``.
* Every file in the slice must parse as JSON (JSONL line-by-line).

The test is deterministic and CI-safe: it reads only the committed slice
and ``desert_truth.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

try:
    from astrid.core.timeline.snapshot_digest import (
        canonical_json_bytes,
    )
except ImportError:  # pragma: no cover - fallback for environments without astrid
    def canonical_json_bytes(obj: object) -> bytes:
        return json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_visualize"
SLICE_DIR = FIXTURE_DIR / "desert_slice"
TRUTH_PATH = FIXTURE_DIR / "desert_truth.json"

ASSEMBLY_SHA256 = "87fd29d84ec0cf837dd1534f50e2c7a683a51a8ffa93ef50706c6585739e34ef"
REGISTRY_SHA256 = "514e6020af06a289764f6d1ab282619f49b0021e45a0bfa48034a0cc7106fb37"
HEAD_LAST_HASH = "7c90f004cb68f81b72f9b7bf16c598e7dcc0fe0ab02566fe7873da8ebf5f98bc"

# JSON files that must parse whole; JSONL files parse line-by-line.
JSON_FILES = ("assembly.json", "assembly.head.json", "registry.json")
JSONL_FILES = ("assembly.jsonl",)


def _canonical_sha256(path: Path) -> str:
    with path.open(encoding="utf-8") as fh:
        obj = json.load(fh)
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


def _load_truth() -> dict:
    with TRUTH_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_truth_fixture_exists() -> None:
    assert TRUTH_PATH.is_file(), f"missing truth fixture: {TRUTH_PATH}"


def test_assembly_sha256_is_canonical_json_digest() -> None:
    expected = _canonical_sha256(SLICE_DIR / "assembly.json")
    assert expected == ASSEMBLY_SHA256, (
        "slice assembly.json no longer canonicalizes to the frozen assembly_sha256"
    )
    assert _load_truth()["hashes"]["assembly_sha256"] == ASSEMBLY_SHA256


def test_registry_sha256_is_canonical_json_digest() -> None:
    expected = _canonical_sha256(SLICE_DIR / "registry.json")
    assert expected == REGISTRY_SHA256, (
        "slice registry.json no longer canonicalizes to the frozen registry_sha256"
    )
    assert _load_truth()["hashes"]["registry_sha256"] == REGISTRY_SHA256


def test_head_last_hash_matches_slice() -> None:
    truth_hash = _load_truth()["hashes"]["head_last_hash"]
    assert truth_hash == HEAD_LAST_HASH

    jsonl_path = SLICE_DIR / "assembly.jsonl"
    lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "assembly.jsonl is empty"

    last_event = json.loads(lines[-1])
    assert last_event["hash"] == HEAD_LAST_HASH, (
        "last assembly.jsonl event hash drifted from frozen head_last_hash"
    )

    head = json.loads((SLICE_DIR / "assembly.head.json").read_text(encoding="utf-8"))
    assert head["last_hash"] == HEAD_LAST_HASH, (
        "assembly.head.json last_hash drifted from frozen head_last_hash"
    )


def test_every_event_hash_and_prev_hash_form_a_chain_from_genesis() -> None:
    from astrid.core.timeline.events.schema import TimelineEvent, with_event_hash

    jsonl_path = SLICE_DIR / "assembly.jsonl"
    previous_hash = None
    with jsonl_path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            assert line.strip(), f"blank event line at {line_no}"
            event = TimelineEvent.from_dict(json.loads(line))
            assert event.prev_hash == previous_hash, (
                f"event line {line_no} prev_hash does not match the previous event"
            )
            recomputed = with_event_hash(event, prev_hash=previous_hash)
            assert event.hash == recomputed.hash, (
                f"event line {line_no} hash does not match its canonical content"
            )
            previous_hash = event.hash


@pytest.mark.parametrize("name", JSON_FILES)
def test_slice_json_files_parse(name: str) -> None:
    path = SLICE_DIR / name
    assert path.is_file(), f"missing slice file: {path}"
    with path.open(encoding="utf-8") as fh:
        json.load(fh)  # raises on malformed JSON


@pytest.mark.parametrize("name", JSONL_FILES)
def test_slice_jsonl_files_parse(name: str) -> None:
    path = SLICE_DIR / name
    assert path.is_file(), f"missing slice file: {path}"
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if line.strip():
                json.loads(line)  # raises on malformed JSON
