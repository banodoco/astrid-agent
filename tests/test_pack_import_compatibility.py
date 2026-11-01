from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.pack import check_pack_imports


def _pack(root: Path, *, declaration: str = "id: example\nastrid_version: '0.1.0'\n") -> Path:
    root.mkdir(parents=True)
    (root / "pack.yaml").write_text(declaration, encoding="utf-8")
    return root


def test_check_pack_imports_accepts_local_modules_and_compatible_version(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example")
    (root / "entry.py").write_text("from .helpers import value\n", encoding="utf-8")
    (root / "helpers.py").write_text("value = 1\n", encoding="utf-8")

    report = check_pack_imports(root, host_version="0.2.0")

    assert report.valid
    assert report.imports == (".helpers",)


def test_check_pack_imports_fails_closed_for_missing_local_module(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example")
    (root / "entry.py").write_text("from .missing import value\n", encoding="utf-8")

    report = check_pack_imports(root, host_version="0.2.0")

    assert not report.valid
    assert any("missing module" in error for error in report.errors)


@pytest.mark.parametrize(
    "statement",
    [
        "from astrid.sdk._private import value\n",
        "from astrid.sdk import _private\n",
    ],
)
def test_check_pack_imports_fails_closed_for_private_sdk_imports(
    tmp_path: Path, statement: str
) -> None:
    root = _pack(tmp_path / "example")
    (root / "entry.py").write_text(statement, encoding="utf-8")

    report = check_pack_imports(root, host_version="0.2.0")

    assert not report.valid
    assert any("forbidden private import" in error for error in report.errors)


def test_check_pack_imports_fails_closed_for_incompatible_version(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example", declaration="id: example\nastrid_version: '1.0.0'\n")

    report = check_pack_imports(root, host_version="0.2.0")

    assert not report.valid
    assert any("incompatible" in error for error in report.errors)


@pytest.mark.parametrize(
    "declaration",
    [
        "id: example\nastrid_version: 'latest'\n",
        "id: example\nastrid_version: null\n",
        "id: first\nid: second\n",
        "- not-a-mapping\n",
        "id: [unterminated\n",
    ],
)
def test_check_pack_imports_fails_closed_for_malformed_declaration(
    tmp_path: Path, declaration: str
) -> None:
    root = _pack(tmp_path / "example", declaration=declaration)

    report = check_pack_imports(root, host_version="0.2.0")

    assert not report.valid
    assert report.errors


def test_check_pack_imports_checks_optional_public_sdk_inventory(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example")
    (root / "entry.py").write_text("from astrid.sdk.missing import value\n", encoding="utf-8")

    report = check_pack_imports(
        root, host_version="0.2.0", public_sdk_modules={"astrid.sdk.dto"}
    )

    assert not report.valid
    assert any("missing SDK module" in error for error in report.errors)
