from __future__ import annotations

import hashlib
import json
import math

import pytest

from astrid.packs.rendering.actions.timeline_visualize.ids import (
    QualifiedRef,
    RootIdMap,
    format_qualified_ref,
    parse_qualified_ref,
)
from astrid.core.timeline.snapshot_digest import (
    canonical_json_bytes,
    sha256_bytes,
    sns_digest,
)


TIMELINE_UUID = "ed70ef66-43da-4182-9f14-69361c6c5e10"
SECOND_TIMELINE_UUID = "01234567-89ab-4def-8123-456789abcdef"
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.mark.parametrize(
    ("ref", "timeline_id", "object_id", "kind"),
    [
        ("TL01", "TL01", None, "TL"),
        ("TL01.SH02", "TL01", "SH02", "SH"),
        ("TL01.RG03", "TL01", "RG03", "RG"),
        ("TL01.CL04", "TL01", "CL04", "CL"),
        ("TL01.AS05", "TL01", "AS05", "AS"),
        ("TL01.TS06", "TL01", "TS06", "TS"),
        ("TL01.SP07", "TL01", "SP07", "SP"),
        ("TL12.CL500", "TL12", "CL500", "CL"),
    ],
)
def test_qualified_ref_round_trip(
    ref: str, timeline_id: str, object_id: str | None, kind: str
) -> None:
    parsed = parse_qualified_ref(ref)

    assert isinstance(parsed, QualifiedRef)
    assert parsed.timeline_id == timeline_id
    assert parsed.object_id == object_id
    assert parsed.kind == kind
    assert str(parsed) == ref


def test_format_qualified_ref_supports_ordinals_ids_and_semantic_kinds() -> None:
    assert format_qualified_ref(1) == "TL01"
    assert format_qualified_ref(1, "CL", 3) == "TL01.CL03"
    assert format_qualified_ref("TL01", "CL03") == "TL01.CL03"
    assert format_qualified_ref("TL01", "clip", 500) == "TL01.CL500"


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "CL03",
        "TL1",
        "TL00",
        "TL001",
        "TL01.CL00",
        "TL01.CL003",
        "TL01.TL02",
        "tl01.CL03",
        "TL01.cl03",
        " TL01.CL03",
        "TL01.CL03 ",
        "TL01..CL03",
        "TL01.TR03",
        "TL٠١.CL03",
        "TL01@00:60",
        "TL01@60:00",
        "TL01@01:60:00",
        "TL01@00:12.1",
        "TL01@00:12.0000",
    ],
)
def test_parse_qualified_ref_rejects_malformed_values(ref: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_qualified_ref(ref)


@pytest.mark.parametrize(
    "ref",
    [
        "TL01@00:12",
        "TL01@00:12.000",
        "TL01@01:02:03",
        "TL01@01:02:03.250",
        "TL99@123:59:59.999",
    ],
)
def test_timestamp_locators_round_trip(ref: str) -> None:
    parsed = parse_qualified_ref(ref)

    assert parsed.is_timestamp
    assert parsed.kind == "timestamp"
    assert parsed.object_id is None
    assert str(parsed) == ref
    assert format_qualified_ref(parsed.timeline_id, timestamp=parsed.timestamp) == ref


def test_root_id_map_rejects_duplicate_authored_identity_and_display_id() -> None:
    root = RootIdMap()
    clip_identity = (TIMELINE_UUID, "clip", "plant-frame-2")
    root.add(clip_identity, "TL01.CL03")

    with pytest.raises(ValueError, match="duplicate semantic identity"):
        root.add(clip_identity, "TL01.CL04")
    with pytest.raises(ValueError, match="duplicate display id"):
        root.add((TIMELINE_UUID, "clip", "plant-frame-3"), "TL01.CL03")


def test_root_id_map_allows_same_authored_id_across_kinds_and_timelines() -> None:
    root = RootIdMap()
    root.add((TIMELINE_UUID, "clip", "shared"), "TL01.CL01")
    root.add((TIMELINE_UUID, "asset", "shared"), "TL01.AS01")
    root.add((SECOND_TIMELINE_UUID, "clip", "shared"), "TL02.CL01")

    assert root.lookup((TIMELINE_UUID, "clip", "shared")) == "TL01.CL01"
    assert root.lookup((TIMELINE_UUID, "asset", "shared")) == "TL01.AS01"
    assert root.lookup((SECOND_TIMELINE_UUID, "clip", "shared")) == "TL02.CL01"


def test_child_id_map_is_a_byte_for_byte_immutable_copy() -> None:
    root = RootIdMap()
    first = (TIMELINE_UUID, "timeline", "plant-growth-storyboard")
    second = (TIMELINE_UUID, "clip", "plant-frame-2")
    root.add(first, "TL01")
    root.add(second, "TL01.CL03")

    child = root.copy()

    assert child.sealed
    assert list(child.entries.items()) == list(root.entries.items())
    assert child.lookup(second) == "TL01.CL03"
    with pytest.raises(TypeError, match="immutable"):
        child.add((TIMELINE_UUID, "clip", "plant-frame-3"), "TL01.CL04")

    root.add((TIMELINE_UUID, "clip", "plant-frame-4"), "TL01.CL05")
    assert list(child.entries.items()) != list(root.entries.items())
    with pytest.raises(KeyError):
        child.lookup((TIMELINE_UUID, "clip", "plant-frame-4"))


def test_root_id_map_rejects_kind_and_timeline_allocation_conflicts() -> None:
    root = RootIdMap()
    root.add((TIMELINE_UUID, "clip", "one"), "TL01.CL01")

    with pytest.raises(ValueError, match="requires a CL"):
        root.add((TIMELINE_UUID, "clip", "two"), "TL01.AS02")
    with pytest.raises(ValueError, match="already allocated as TL01"):
        root.add((TIMELINE_UUID, "clip", "two"), "TL02.CL02")


def test_root_id_map_preserves_opaque_runtime_timeline_id() -> None:
    timeline_id = "astrid-v1-timeline-full-20260912"
    root = RootIdMap()

    root.add((timeline_id, "timeline", timeline_id), "TL01")
    root.add((timeline_id, "clip", "clip-1"), "TL01.CL01")

    assert root.lookup((timeline_id, "timeline", timeline_id)) == "TL01"
    assert root.lookup((timeline_id, "clip", "clip-1")) == "TL01.CL01"


def _snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "project_slug": "desert-plant-growth",
        "timeline_uuid": TIMELINE_UUID,
        "timeline_ulid": "01KYPVKMW5STB4W6FE05ED8242",
        "head_version": 159,
        "head_last_event_id": "01KZS6CCD73SYEC924B5XR12XG",
        "head_last_hash": HASH_A,
        "assembly_sha256": HASH_B,
        "registry_sha256": "c" * 64,
        "transcript_sha256": "d" * 64,
        "media_hashes": {"plant-frame-2": "e" * 64, "music": "f" * 64},
    }
    snapshot.update(overrides)
    return snapshot


def test_sns_digest_is_stable_and_uses_the_exact_canonical_envelope() -> None:
    snapshot = _snapshot()
    expected_bytes = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

    assert sns_digest(snapshot) == f"SNS:{hashlib.sha256(expected_bytes).hexdigest()}"
    assert sns_digest(snapshot) == sns_digest(dict(snapshot))


def test_sns_digest_excludes_wall_clock_metadata() -> None:
    first = _snapshot(frozen_at="2026-08-11T10:00:00Z", created_at="2026-08-11T10:00:01Z")
    second = _snapshot(frozen_at="2036-01-01T00:00:00Z", created_at="2036-01-01T00:00:01Z")

    assert sns_digest(first) == sns_digest(second)


def test_sns_digest_accepts_the_explicit_empty_event_head() -> None:
    empty_head = _snapshot(
        head_version=0,
        head_last_event_id=None,
        head_last_hash=None,
    )

    assert sns_digest(empty_head).startswith("SNS:")
    with pytest.raises(ValueError, match="version-zero event head"):
        sns_digest(_snapshot(head_version=0, head_last_event_id=None))


def test_sns_digest_ignores_mapping_insertion_order_and_sorts_media_hashes() -> None:
    first = _snapshot(media_hashes={"z-asset": HASH_A, "a-asset": HASH_B})
    second_items = list(reversed(list(first.items())))
    second = dict(second_items)
    second["media_hashes"] = {"a-asset": HASH_B, "z-asset": HASH_A}

    assert sns_digest(first) == sns_digest(second)


def test_absent_and_null_optional_transcript_hash_have_the_same_identity() -> None:
    absent = _snapshot()
    absent.pop("transcript_sha256")
    null = _snapshot(transcript_sha256=None)

    assert sns_digest(absent) == sns_digest(null)


def test_canonical_json_and_sns_reject_non_finite_numbers() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            canonical_json_bytes({"timing": value})

    with pytest.raises(ValueError, match="head_version"):
        sns_digest(_snapshot(head_version=math.nan))


def test_sha256_bytes_is_bare_lowercase_hex() -> None:
    assert sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sns_digest_preserves_opaque_runtime_timeline_id() -> None:
    fields = _snapshot(timeline_uuid="astrid-v1-timeline-full-20260912")
    digest = sns_digest(fields)

    assert digest.startswith("SNS:")
    assert fields["timeline_uuid"] == "astrid-v1-timeline-full-20260912"


def test_sns_rejects_unrecognized_identity_fields_instead_of_silently_omitting_them() -> None:
    with pytest.raises(ValueError, match="unexpected snapshot field"):
        sns_digest(_snapshot(new_identity_fact="must-not-be-ignored"))


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", 2])
def test_sns_requires_the_integer_v1_envelope(schema_version: object) -> None:
    with pytest.raises(ValueError, match="schema_version must be 1"):
        sns_digest(_snapshot(schema_version=schema_version))
