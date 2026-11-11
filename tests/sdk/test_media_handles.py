from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.codex import _build_codex_prompt
from astrid.sdk.media_handles import MediaHandleError, apply_media_handles, resolve_media_handle
from astrid.sdk.results import InvocationResult


IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _ok(data):
    return SimpleNamespace(ok=True, data=data, error=None)


class _Runtime:
    def __init__(self) -> None:
        self.associations: list[dict[str, object]] = []
        self.reference = {
            "reference_id": "ref-1",
            "name": "Presenter",
            "media_references": [
                {"association_id": "assoc-1", "media_id": IMAGE, "ordinal": 0, "is_primary": True}
            ],
        }
        self.references = SimpleNamespace(
            show=lambda project, ref: _ok(self.reference),
            associate=self._associate,
        )
        self.media = SimpleNamespace(
            show=lambda project, digest: _ok(
                {"digest": digest, "filename": "generated_images", "media_type": "image/png", "size": 11}
            )
        )

    def _associate(self, project, ref, *, media_id, role, metadata=None, idempotency_key=None):
        self.associations.append(
            {"project": project, "ref": ref, "media_id": media_id, "role": role,
             "metadata": metadata, "idempotency_key": idempotency_key}
        )
        return _ok({})


def _capability():
    return SimpleNamespace(
        id="generation.generate_image_codex",
        inputs=(
            SimpleNamespace(name="image_ref", type="file"),
            SimpleNamespace(name="style_ref", type="file"),
            SimpleNamespace(name="brand_ref", type="file"),
        ),
    )


def test_invocation_result_outputs_are_ordinal_handles_and_skip_thumbnails() -> None:
    result = InvocationResult(
        capability_id="demo",
        capability_type="executor",
        native_kind="action",
        ok=True,
        outputs={
            "managed_outputs": [
                {"output_port": "thumbnail", "role": "thumbnail", "ordinal": 0},
                {"output_port": "images", "role": "result", "ordinal": 1, "digest": IMAGE},
                {"output_port": "images", "role": "result", "ordinal": 0, "digest": OTHER},
            ]
        },
    )

    assert [row["digest"] for row in result.output_rows()] == [OTHER, IMAGE]
    assert result.output("images", 1)["digest"] == IMAGE
    with pytest.raises(LookupError, match=r"images\[result\]"):
        result.output("images", 2)


def test_reference_and_digest_handles_resolve_to_named_project_descriptors() -> None:
    runtime = _Runtime()

    descriptor, lineage = resolve_media_handle(runtime, "demo", "ref:Presenter")
    assert descriptor == {
        "digest": IMAGE,
        "filename": "Presenter.png",
        "media_type": "image/png",
        "size_bytes": 11,
    }
    assert lineage["reference"]["id"] == "ref-1"

    digest_descriptor, digest_lineage = resolve_media_handle(runtime, "demo", IMAGE)
    assert digest_descriptor["digest"] == IMAGE
    assert digest_lineage == {"handle": IMAGE, "digest": IMAGE}


def test_reference_roles_expand_to_distinct_file_ports() -> None:
    runtime = _Runtime()
    values, lineage, links = apply_media_handles(
        runtime,
        "demo",
        _capability(),
        {"references": [
            {"ref": OTHER, "role": "source"},
            {"ref": "Presenter", "role": "character", "depicts": True},
        ]},
    )

    assert values["image_ref"]["digest"] == OTHER
    assert values["style_ref"]["digest"] == IMAGE
    assert [entry["role"] for entry in lineage] == ["source", "character"]
    assert links[0]["role"] == "character"
    with pytest.raises(MediaHandleError, match="same image"):
        apply_media_handles(
            runtime,
            "demo",
            _capability(),
            {"references": [
                {"ref": IMAGE, "role": "source"},
                {"ref": IMAGE, "role": "character"},
            ]},
        )


def test_repeatable_file_handles_resolve_in_order() -> None:
    runtime = _Runtime()
    capability = SimpleNamespace(
        id="pixel.strip",
        inputs=(SimpleNamespace(name="frame", type="file"),),
    )

    values, lineage, _links = apply_media_handles(
        runtime, "demo", capability, {"frame": [IMAGE, OTHER]}
    )

    assert [row["digest"] for row in values["frame"]] == [IMAGE, OTHER]
    assert [(row["port"], row["index"], row["handle"]) for row in lineage] == [
        ("frame", 0, IMAGE),
        ("frame", 1, OTHER),
    ]


def test_codex_prompt_preserves_the_current_role_for_partial_references(tmp_path) -> None:
    character = tmp_path / "character.png"
    character.write_bytes(b"png")

    prompt = _build_codex_prompt({"prompt": "a new scene", "style_ref": str(character)})

    assert "1) the character/style reference" in prompt
    assert "source image" not in prompt


def test_timeline_asset_resolver_carries_pixel_sprites_report() -> None:
    import hashlib
    import json

    from astrid.sdk.media_handles import resolve_timeline_asset

    def digest(blob: bytes) -> str:
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    report = json.dumps({"pieces": [{"index": 0, "drawn_block": 3.5,
                                     "lattice": {"detected": True, "block": 3.5},
                                     "outline_integrity": {"fraction": 0.70}}]}).encode()
    sprite = b"sprite-png"
    rows = [
        {"run_id": "r-00", "task_id": "t-00", "output_port": "sprites", "ordinal": 0, "role": "result",
         "filename": "sprite-00.png", "media_type": "image/png", "size": len(sprite), "digest": digest(sprite)},
        {"run_id": "r-00", "task_id": "t-00", "output_port": "report", "ordinal": 0, "role": "auxiliary",
         "filename": "report.json", "media_type": "application/json", "size": len(report), "digest": digest(report)},
    ]
    objects = {digest(sprite): sprite, digest(report): report}
    media = {row["digest"]: {"digest": row["digest"], "filename": row["filename"],
                             "media_type": row["media_type"], "size": row["size"]} for row in rows}
    runtime = SimpleNamespace(
        runs=SimpleNamespace(show=lambda run_id: _ok({"task_ids": ["t-00"]})),
        tasks=SimpleNamespace(list_managed_outputs=lambda task_id: _ok(rows)),
        media=SimpleNamespace(show=lambda project, d: _ok(media[d]), read_bytes=lambda d: objects[d]),
    )
    entry = resolve_timeline_asset(runtime, "demo", "run:r-00/sprites#0")
    assert entry["key"] == "sprite-00"
    assert entry["pixel_provenance"] == {
        "executor": "pixel.sprites", "run_id": "r-00", "lattice": {"detected": True, "block": 3.5},
        "drawn_block": 3.5, "outline_integrity": {"fraction": 0.70},
    }


@pytest.mark.parametrize("answer", ["variant", "not_found", "conflict"])
def test_timeline_asset_resolver_asks_runtime_for_the_task_output_catalog_variant(answer) -> None:
    """A task output carries its catalog ids only when Runtime binds the exact output to one variant."""
    from astrid.sdk.media_handles import resolve_timeline_asset
    from astrid.sdk.workspace_client import WorkspaceClientError

    row = {"association_id": "managed-output-1", "run_id": "r-1", "task_id": "t-1", "output_port": "generated_images",
           "ordinal": 1, "role": "output", "filename": "cpu-1.png", "media_type": "video/mp4", "size": 11,
           "digest": IMAGE, "object_id": IMAGE, "lifecycle": {"state": "available", "version": 1}}
    asked = []

    def selection(project, selector):
        asked.append((project, selector))
        if answer == "not_found":
            raise WorkspaceClientError(404, "not_found", "output has no authoritative registered variant", {})
        if answer == "conflict":
            raise WorkspaceClientError(409, "conflict", "selected output is not an exact completed settlement", {})
        return {"asset": "variant-1", "generation_id": "generation-1", "variant_id": "variant-1",
                "object_id": IMAGE, "catalog_digest": "sha256:" + "c" * 64}

    runtime = SimpleNamespace(
        runs=SimpleNamespace(show=lambda run_id: _ok({"task_ids": ["t-1"]})),
        tasks=SimpleNamespace(list_managed_outputs=lambda task_id: _ok([row])),
        media=SimpleNamespace(show=lambda project, d: _ok({"digest": d, "filename": "cpu-1.mp4",
                                                           "media_type": "video/mp4", "size": 11})),
        _remote=SimpleNamespace(_transport=SimpleNamespace(get_project_media_selection=selection)),
    )
    for handle in ("task:t-1/generated_images#1", "run:r-1/generated_images#1"):
        entry = resolve_timeline_asset(runtime, "demo", handle)
        if answer == "variant":
            assert (entry["generationId"], entry["variantId"]) == ("generation-1", "variant-1")
        else:
            assert "generationId" not in entry and "variantId" not in entry
        assert entry["media_id"] == IMAGE
    assert asked == [("demo", {"association_id": "managed-output-1", "object_id": IMAGE, "version": 1})] * 2


def test_timeline_asset_resolver_refuses_a_runtime_selection_for_other_media() -> None:
    from astrid.sdk.media_handles import resolve_timeline_asset

    row = {"association_id": "managed-output-1", "task_id": "t-1", "output_port": "images", "ordinal": 0,
           "role": "output", "digest": IMAGE, "lifecycle": {"version": 2}}
    runtime = SimpleNamespace(
        tasks=SimpleNamespace(list_managed_outputs=lambda task_id: _ok([row])),
        media=SimpleNamespace(show=lambda project, d: _ok({"digest": d, "filename": "a.mp4",
                                                           "media_type": "video/mp4", "size": 11})),
        _remote=SimpleNamespace(_transport=SimpleNamespace(get_project_media_selection=lambda project, selector: {
            "generation_id": "generation-1", "variant_id": "variant-1", "object_id": OTHER})),
    )
    with pytest.raises(MediaHandleError, match="differs from the task output"):
        resolve_timeline_asset(runtime, "demo", "task:t-1/images#0")
