"""Focused validation for generation verbs in pack extensions."""

import sys
import types
from pathlib import Path

import pytest
import yaml

from astrid.core.pack import PackValidationError, discover_packs, load_pack_manifest
from astrid.core.pack.permissions import _normalize_generation_extensions
from astrid.core.generation import verbs as generation_verbs


VERBS = [
    {"name": "enhance", "module": "example.generation", "handler": "enhance"},
    {"name": "animate", "module": "example.motion", "handler": "run"},
]


def test_generation_verbs_preserve_order_and_existing_extension_normalization() -> None:
    normalized = _normalize_generation_extensions(
        {
            "verbs": VERBS,
            "features": ["video_enhance"],
            "modes": [{"id": "edit", "label": "Edit"}],
        },
        path="pack.extensions.generation",
    )

    assert normalized == {
        "verbs": VERBS,
        "features": [{"id": "video_enhance"}],
        "modes": [{"id": "edit", "label": "Edit"}],
    }


@pytest.mark.parametrize(
    ("verbs", "error_path"),
    [
        (None, "pack.extensions.generation.verbs"),
        ("enhance", "pack.extensions.generation.verbs"),
        ({}, "pack.extensions.generation.verbs"),
        ([None], r"pack.extensions.generation.verbs\[0\]"),
        (["enhance"], r"pack.extensions.generation.verbs\[0\]"),
        *[
            (
                [{key: value for key, value in VERBS[0].items() if key != missing}],
                rf"pack.extensions.generation.verbs\[0\].{missing}",
            )
            for missing in ("name", "module", "handler")
        ],
        *[
            (
                [{**VERBS[0], field: value}],
                rf"pack.extensions.generation.verbs\[0\].{field}",
            )
            for field in ("name", "module", "handler")
            for value in (None, 1, "", "  ")
        ],
        ([{**VERBS[0], "unexpected": "value"}], r"pack.extensions.generation.verbs\[0\]"),
        ([{**VERBS[0], 17: "value"}], r"pack.extensions.generation.verbs\[0\]"),
    ],
)
def test_malformed_generation_verbs_raise_at_the_declaration_path(
    verbs: object,
    error_path: str,
) -> None:
    with pytest.raises(PackValidationError, match=error_path):
        _normalize_generation_extensions(
            {"verbs": verbs},
            path="pack.extensions.generation",
        )


@pytest.mark.parametrize("reserved_name", ["image", "video", " image ", "video "])
def test_generation_verbs_reject_reserved_builtin_names(reserved_name: str) -> None:
    verb = {**VERBS[0], "name": reserved_name}
    with pytest.raises(
        PackValidationError,
        match=r"pack.extensions.generation.verbs\[0\].name is reserved",
    ):
        _normalize_generation_extensions(
            {"verbs": [verb]},
            path="pack.extensions.generation",
        )


def test_generation_extensions_still_reject_unknown_keys() -> None:
    with pytest.raises(
        PackValidationError,
        match=r"pack.extensions.generation has unknown field\(s\): unknown",
    ):
        _normalize_generation_extensions(
            {"verbs": VERBS, "unknown": []},
            path="pack.extensions.generation",
        )


def _write_v3_pack(root: Path, pack_id: str, verbs: list[dict[str, str]]) -> Path:
    pack_root = root / pack_id
    pack_root.mkdir(parents=True)
    (pack_root / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "id": pack_id,
                "name": pack_id,
                "version": "1.0.0",
                "documents": {},
                "extensions": {"generation": {"verbs": verbs}},
            }
        ),
        encoding="utf-8",
    )
    return pack_root


def test_v3_pack_discovery_preserves_generation_verb_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs_root = tmp_path / "astrid" / "packs"
    packs_root.mkdir(parents=True)
    declarations = {
        "wan2gp": [
            {"name": "generate_video", "module": "fixture_generation_handlers", "handler": "wan"}
        ],
        "vibecomfy": [
            {
                "name": "video_enhance",
                "module": "fixture_generation_handlers",
                "handler": "enhance",
            },
            {
                "name": "character_animation",
                "module": "fixture_generation_handlers",
                "handler": "animate",
            },
        ],
    }
    for pack_id, pack_verbs in declarations.items():
        _write_v3_pack(packs_root, pack_id, pack_verbs)

    discovered = discover_packs(packs_root)
    assert {pack.id: pack.extensions["generation"]["verbs"] for pack in discovered} == declarations
    assert all(
        load_pack_manifest(packs_root / pack_id / "pack.yaml").extensions["generation"]["verbs"]
        == pack_verbs
        for pack_id, pack_verbs in declarations.items()
    )

    handlers = types.ModuleType("fixture_generation_handlers")
    handlers.wan = lambda **kwargs: kwargs
    handlers.enhance = lambda **kwargs: kwargs
    handlers.animate = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "fixture_generation_handlers", handlers)
    monkeypatch.setattr("astrid.core.foundation.paths.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "astrid.core.pack.discover_packs",
        lambda root=None: discover_packs(packs_root if root is None else root),
    )
    monkeypatch.setattr(
        "astrid.core.pack.source_setup.active_source_inventory",
        lambda: types.SimpleNamespace(sources=()),
    )
    monkeypatch.setattr(generation_verbs, "_plugins_loaded", False)
    monkeypatch.setattr(generation_verbs, "_verbs", {})

    generation_verbs.load_generation_verb_plugins()

    assert generation_verbs.get_verb("generate_video") is handlers.wan
    assert generation_verbs.get_verb("video_enhance") is handlers.enhance
    assert generation_verbs.get_verb("character_animation") is handlers.animate


@pytest.mark.parametrize(
    ("extensions", "error_path"),
    [
        ({"unknown": {}}, "pack.extensions has unknown field"),
        ({"generation": {"unknown": []}}, "pack.extensions.generation has unknown field"),
        ({"generation": {"verbs": [{"name": "video", "module": "x", "handler": "run"}]}},
         r"pack.extensions.generation.verbs\[0\].name is reserved"),
        ({"generation": {"verbs": [{"name": "broken", "module": "x"}]}},
         r"pack.extensions.generation.verbs\[0\].handler"),
    ],
)
def test_v3_loader_keeps_generation_extension_validation(
    tmp_path: Path, extensions: dict, error_path: str
) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "id": "demo",
                "name": "Demo",
                "version": "1.0.0",
                "documents": {},
                "extensions": extensions,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PackValidationError, match=error_path):
        load_pack_manifest(root / "pack.yaml")
