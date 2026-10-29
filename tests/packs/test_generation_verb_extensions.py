"""Focused validation for generation verbs in pack extensions."""

import pytest

from astrid.core.pack import PackValidationError
from astrid.core.pack.permissions import _normalize_generation_extensions


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
