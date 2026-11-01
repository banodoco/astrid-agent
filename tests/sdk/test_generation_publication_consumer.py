from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core._shared.result_manifest import harvest_staged_outputs
from astrid.core.contracts.schema import Output
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.sdk.invocation import (
    _automatic_generation_intent,
    _generation_publish_effect,
    _kernel_invoke,
    _validate_generation_intent,
)
from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.results import Capability, _join_managed_generation_outputs


def _capability() -> Capability:
    outputs = (
        Output(name="generated_videos", type="file", artifact_type="video/clip"),
        Output(name="video_manifest", type="file", artifact_type=None),
        Output(name="thumbnail", type="file", artifact_type="image"),
    )
    return Capability(
        id="generation.generate_video",
        capability_type="executor",
        native_kind="executor",
        handle=SimpleNamespace(),
        outputs=outputs,
        definition={
            "outputs": [
                {"name": "generated_videos", "type": "file", "artifact_type": "video/clip"},
                {"name": "video_manifest", "type": "file", "artifact_type": None},
            ]
        },
    )


def test_effect_preserves_group_selector_order_and_schema_port() -> None:
    intent = {
        "version": 1,
        "modality": "video",
        "partial_success_policy": "allow",
        "groups": [
            {"group_key": "second", "selectors": [{"selector": "b", "ordinal": 7, "variant_key": "v7"}]},
            {"group_key": "first", "selectors": [{"selector": "a", "ordinal": 2, "variant_key": "v2"}]},
        ],
    }
    effect = _generation_publish_effect(_capability(), project="project-1", generation_intent=intent)
    assert effect == {
        "effect_type": "generation.publish_v1",
        "target_id": "project-1",
        "payload": {
            "version": 1,
            "modality": "video",
            "generation_type": "generation.generate_video",
            "metadata": {},
            "partial_success_policy": "allow",
            "groups": [
                {"group_key": "second", "selectors": [{"selector": "b", "ordinal": 7, "variant_key": "v7", "output_port": "generated_videos"}]},
                {"group_key": "first", "selectors": [{"selector": "a", "ordinal": 2, "variant_key": "v2", "output_port": "generated_videos"}]},
            ],
        },
    }


def test_generation_metadata_is_carried_into_the_published_generation() -> None:
    intent = {
        "version": 1,
        "modality": "video",
        "partial_success_policy": "allow",
        "groups": [
            {"group_key": "main", "selectors": [{"selector": "s", "ordinal": 0, "variant_key": "v"}]}
        ],
        "metadata": {
            "shot_id": "shot-17",
            "scene_id": "scene-3",
            "labels": ["hero", "wide"],
            "review": {"selected": False},
        },
    }

    validated = _validate_generation_intent(intent, modality="video")
    effect = _generation_publish_effect(
        _capability(), project="project-1", generation_intent=validated
    )

    assert validated["metadata"] == intent["metadata"]
    assert effect["payload"]["metadata"] == intent["metadata"]


@pytest.mark.parametrize(
    "metadata, match",
    [
        ("shot-17", "must be an object"),
        ({"shot_id": float("nan")}, "finite JSON values"),
        ({"shot_id": "x" * (16 * 1024)}, "at most 16384 bytes"),
    ],
)
def test_generation_metadata_is_small_and_json_safe(metadata, match) -> None:
    intent = {
        "version": 1,
        "modality": "video",
        "partial_success_policy": "allow",
        "groups": [
            {"group_key": "main", "selectors": [{"selector": "s", "ordinal": 0, "variant_key": "v"}]}
        ],
        "metadata": metadata,
    }
    with pytest.raises(CapabilityValidationError, match=match):
        _validate_generation_intent(intent, modality="video")


def test_kernel_admission_predeclares_typed_effect_and_omits_it_without_intent() -> None:
    calls = []

    class Tasks:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(ok=True, data={"run_id": "r", "task_id": "t", "attempt_id": "a"})

    client = SimpleNamespace(tasks=Tasks())
    intent = {
        "version": 1, "modality": "video", "partial_success_policy": "reject",
        "groups": [{"group_key": "g", "selectors": [{"selector": "s", "ordinal": 0, "variant_key": "v"}]}],
        "metadata": {"shot_id": "shot-17", "take": 2},
    }
    _kernel_invoke(_capability(), kind="executor", project="project-1", inputs={}, outputs={}, generation_intent=intent, _client=client)
    _kernel_invoke(_capability(), kind="executor", project="project-1", inputs={}, outputs={}, _client=client)
    assert calls[0]["settlement_effect"]["effect_type"] == "generation.publish_v1"
    assert calls[0]["generation_intent"]["metadata"] == {"shot_id": "shot-17", "take": 2}
    assert calls[0]["settlement_effect"]["payload"]["metadata"] == {
        "shot_id": "shot-17", "take": 2
    }
    assert calls[0]["settlement_effect"]["payload"]["groups"][0]["selectors"][0]["output_port"] == "generated_videos"
    assert "settlement_effect" not in calls[1]


def _managed_output(generation_id: str | None) -> dict[str, object]:
    return {
        "association_id": f"association-{generation_id or 'generic'}",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "output_port": "generated_videos",
        "selector": {"group_key": "g", "variant_key": "v"},
        "ordinal": 0,
        "role": "result",
        "filename": "generated.mp4",
        "media_type": "video/mp4",
        "size": 3,
        "digest": "sha256:" + "a" * 64,
        "durability": "durable",
        "generation_id": generation_id,
    }


def test_effect_resolves_each_real_sdk_modality_port_and_excludes_extras() -> None:
    for modality, port, artifact in (
        ("image", "generated_images", "image"),
        ("video", "generated_videos", "video/clip"),
        ("audio", "generated_audio", "audio"),
    ):
        capability = Capability(
            id=f"generation.generate_{modality}",
            capability_type="executor",
            native_kind="executor",
            handle=SimpleNamespace(),
            outputs=(
                Output(name=port, type="file", artifact_type=artifact),
                Output(name=f"{modality}_manifest", type="file", artifact_type=None),
                Output(name="auxiliary", type="file", artifact_type="application/json"),
            ),
            # This is the real SDK Capability shape: definition is a mapping.
            definition={"outputs": [{"name": "wrong", "type": "file", "artifact_type": artifact}]},
        )
        intent = {
            "version": 1,
            "modality": modality,
            "partial_success_policy": "allow",
            "groups": [{"group_key": "g", "selectors": [{"selector": "s", "ordinal": 0, "variant_key": "v"}]}],
        }
        effect = _generation_publish_effect(capability, project="project-1", generation_intent=intent)
        assert effect["payload"]["groups"][0]["selectors"][0]["output_port"] == port


def test_effect_uses_typed_specialized_output_ports() -> None:
    for capability_id, modality, port, artifact in (
        ("fal.fal_foley", "audio", "audio", "audio"),
        ("fal.h3_video", "video", "generated_videos", "video/clip"),
        ("vibecomfy.character_animation", "video", "animated_video", "video"),
        ("vibecomfy.video_enhance", "video", "enhanced_video", "video"),
    ):
        capability = Capability(
            id=capability_id,
            capability_type="executor",
            native_kind="executor",
            handle=SimpleNamespace(),
            outputs=(Output(name=port, type="file", artifact_type=artifact),),
        )
        effect = _generation_publish_effect(
            capability,
            project="project-1",
            generation_intent={
                "version": 1,
                "modality": modality,
                "partial_success_policy": "reject",
                "groups": [{"group_key": "main", "selectors": [{"selector": "main-0", "ordinal": 0, "variant_key": "original"}]}],
            },
        )
        assert effect["payload"]["groups"][0]["selectors"][0]["output_port"] == port


def test_automatic_generation_intent_is_bounded_and_leaves_opaque_prompt_files_explicit() -> None:
    assert _automatic_generation_intent(
        _capability(), {}, modality="video"
    ) == {
        "version": 1,
        "modality": "video",
        "partial_success_policy": "reject",
        "groups": [{
            "group_key": "main",
            "selectors": [{
                "selector": "main-0",
                "ordinal": 0,
                "variant_key": "original",
                "required": True,
            }],
        }],
    }
    openai = SimpleNamespace(id="generation.generate_image_openai", capability_type="executor", inputs=())
    assert _automatic_generation_intent(openai, {}, modality="image") is None
    generic = SimpleNamespace(id="vibecomfy.run", capability_type="executor", inputs=())
    assert _automatic_generation_intent(generic, {}, modality=None) is None


def test_generic_vibe_explicit_intent_uses_its_single_untyped_file_output() -> None:
    capability = Capability(
        id="vibecomfy.run",
        capability_type="executor",
        native_kind="executor",
        handle=SimpleNamespace(),
        outputs=(
            Output(name="rendered", type="file", artifact_type=None),
            Output(name="result_manifest", type="file", artifact_type=None),
        ),
    )
    effect = _generation_publish_effect(
        capability,
        project="project-1",
        generation_intent={
            "version": 1,
            "modality": "image",
            "partial_success_policy": "reject",
            "groups": [{
                "group_key": "main",
                "selectors": [{
                    "selector": "main-0",
                    "ordinal": 0,
                    "variant_key": "original",
                }],
            }],
        },
    )
    assert effect["payload"]["groups"][0]["selectors"][0]["output_port"] == "rendered"


def test_managed_generation_join_deduplicates_and_removes_staging_paths() -> None:
    row = _managed_output("generation-1")
    payload = {
        "image_paths": ["/tmp/astrid-attempt-dead/outputs/generated.mp4"],
        "run_dir": "/tmp/astrid-attempt-dead",
        "manifest": {"outputs": [dict(row)]},
    }
    joined = _join_managed_generation_outputs(payload, [dict(row)])
    assert len(joined["manifest"]["outputs"]) == 1
    assert joined["image_paths"] == []
    assert joined["run_dir"] is None


def test_managed_generation_join_replaces_legacy_copy_with_canonical_row() -> None:
    row = _managed_output("generation-1")
    legacy = {
        "name": "generated_videos",
        "ordinal": 0,
        "content_hash": row["digest"],
        "path": "/tmp/astrid-attempt-dead/outputs/generated.mp4",
    }
    joined = _join_managed_generation_outputs(
        {"manifest": {"outputs": [legacy, dict(legacy)]}},
        [dict(row)],
    )
    assert joined["manifest"]["outputs"] == [row]
    assert "path" not in joined["manifest"]["outputs"][0]


def _host_record() -> SimpleNamespace:
    return SimpleNamespace(
        definition=SimpleNamespace(
            outputs=(
                SimpleNamespace(name="generated_videos", type="file", artifact_type="video/clip"),
                SimpleNamespace(name="video_manifest", type="file", artifact_type=None),
                SimpleNamespace(name="aux", type="file", artifact_type="application/json"),
            )
        )
    )


def _descriptor(root, name: str, value: bytes, ordinal: int, **metadata):
    path = root / f"{name}-{ordinal}.bin"
    path.write_bytes(value)
    import hashlib

    return {
        "name": name,
        "path": str(path),
        "ordinal": ordinal,
        "content_hash": "sha256:" + hashlib.sha256(value).hexdigest(),
        "bytes": len(value),
        "role": "result",
        "is_primary": ordinal == 0,
        "ordinal_explicit": True,
        **metadata,
    }


def _intent(policy: str = "reject", *, collision: bool = False):
    return {
        "version": 1,
        "modality": "video",
        "partial_success_policy": policy,
        "groups": [
            {"group_key": "left", "selectors": [{"selector": "left-0", "ordinal": 0, "variant_key": "left-v"}]},
            {"group_key": "right", "selectors": [{"selector": "right-1", "ordinal": 1 if not collision else 0, "variant_key": "right-v"}]},
        ],
    }


def test_host_binds_out_of_order_generated_outputs_by_ordinal_and_selector(tmp_path) -> None:
    intent = _intent()
    host = GenericPackHost.__new__(GenericPackHost)
    outputs = host._typed_outputs(
        _host_record(),
        [
            _descriptor(tmp_path, "generated_videos", b"right", 1, selector={"group_key": "right", "variant_key": "right-v"}, producer={"source": "p"}),
            _descriptor(tmp_path, "generated_videos", b"left", 0, selector={"group_key": "left", "variant_key": "left-v"}, provenance={"trace": "t"}),
            _descriptor(tmp_path, "video_manifest", b"manifest", 9, role="auxiliary"),
            _descriptor(tmp_path, "aux", b"aux", 3, role="auxiliary"),
        ],
        tmp_path,
        generation_intent=intent,
    )
    assert [(item["group_key"], item["variant_key"], item["selector"], item["ordinal"]) for item in outputs[:2]] == [
        ("right", "right-v", {"group_key": "right", "variant_key": "right-v"}, 1),
        ("left", "left-v", {"group_key": "left", "variant_key": "left-v"}, 0),
    ]
    assert outputs[0]["producer"] == {"source": "p"}
    assert outputs[1]["provenance"] == {"trace": "t"}
    assert "group_key" not in outputs[2]
    assert "output_port" not in outputs[3]


def test_host_generation_policy_zero_and_ambiguous_binding_fail_closed(tmp_path) -> None:
    host = GenericPackHost.__new__(GenericPackHost)
    one = _descriptor(tmp_path, "generated_videos", b"one", 0, selector={"group_key": "left", "variant_key": "left-v"})
    with pytest.raises(HostError, match="reject policy"):
        host._typed_outputs(_host_record(), [one], tmp_path, generation_intent=_intent("reject"))
    allowed = host._typed_outputs(_host_record(), [one], tmp_path, generation_intent=_intent("allow"))
    assert len(allowed) == 1 and allowed[0]["group_key"] == "left"
    with pytest.raises(HostError, match="no successful"):
        host._typed_outputs(_host_record(), [], tmp_path, generation_intent=_intent("allow"))

    collision = _descriptor(tmp_path, "generated_videos", b"collision", 0)
    with pytest.raises(HostError, match="unauthorized ambiguous"):
        host._typed_outputs(_host_record(), [collision], tmp_path, generation_intent=_intent("allow", collision=True))
    mismatch = _descriptor(tmp_path, "generated_videos", b"mismatch", 0, selector={"group_key": "right", "variant_key": "right-v"})
    with pytest.raises(HostError, match="no successful|unauthorized"):
        host._typed_outputs(_host_record(), [mismatch], tmp_path, generation_intent=_intent("allow"))
    string_selector = _descriptor(tmp_path, "generated_videos", b"string", 0, selector="left-0")
    with pytest.raises(HostError, match="must be an object"):
        host._typed_outputs(_host_record(), [string_selector], tmp_path, generation_intent=_intent("allow"))


def test_host_rejects_positional_ordinal_injected_by_manifest_harvest(tmp_path) -> None:
    host = GenericPackHost.__new__(GenericPackHost)
    positional = _descriptor(
        tmp_path,
        "generated_videos",
        b"positional",
        0,
        selector={"group_key": "left", "variant_key": "left-v"},
        ordinal_explicit=False,
    )
    with pytest.raises(HostError, match="original ordinal"):
        host._typed_outputs(_host_record(), [positional], tmp_path, generation_intent=_intent("allow"))


def test_host_rejects_manifest_ordinal_omission_after_harvest(tmp_path) -> None:
    spool = tmp_path / "outputs"
    spool.mkdir()
    data = b"manifest-derived generation output"
    (spool / "clip.mp4").write_bytes(data)
    import hashlib
    import json

    (spool / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "video",
                "inputs": {},
                "outputs": [
                    {
                        "path": "clip.mp4",
                        "name": "generated_videos",
                        "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                        "role": "result",
                        "is_primary": True,
                    }
                ],
                "created": "2026-09-11T00:00:00Z",
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )

    harvested = harvest_staged_outputs(spool, require=True)
    assert harvested[0]["ordinal"] == 0
    assert harvested[0]["ordinal_explicit"] is False

    host = GenericPackHost.__new__(GenericPackHost)
    with pytest.raises(HostError, match="original ordinal"):
        host._typed_outputs(
            _host_record(), harvested, spool, generation_intent=_intent("allow")
        )


def test_host_upload_keeps_runtime_selector_object_wire_safe(tmp_path) -> None:
    host = GenericPackHost.__new__(GenericPackHost)
    host.client = SimpleNamespace(
        INLINE_SETTLEMENT_OUTPUTS=True,
        upload_object=lambda *args, **kwargs: SimpleNamespace(digest="sha256:" + "0" * 64, size=1),
    )
    descriptor = _descriptor(
        tmp_path, "generated_videos", b"wire", 0,
        output_port="generated_videos",
        group_key="left",
        variant_key="left-v",
        selector={"group_key": "left", "variant_key": "left-v"},
    )
    uploaded = host._upload_outputs(
        [{**descriptor, "artifact_type": "video/clip"}], project_id="project-1"
    )
    assert uploaded[0]["selector"] == {"group_key": "left", "variant_key": "left-v"}
    assert not isinstance(uploaded[0]["selector"], str)
