"""Pack import policy, exercised through the one checker both pack callers use.

Restored from the Plan 06 audit archive and retargeted from the deleted
``astrid.core.pack.check_pack_imports`` to ``astrid.pack_imports.check_pack_imports``.
Dropped (they belonged solely to the deleted duplicate, which also validated
``pack.yaml`` and host versions): missing-local-module, incompatible-version and
malformed-declaration cases; manifest and version validation now live in the loader.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from astrid import pack_imports
from astrid.core.pack import install, loader
from astrid.pack_imports import ImportContract, check_pack_imports

MANIFEST = {"id": "example", "dependencies": {}}


def _pack(root: Path, source: str) -> Path:
    root.mkdir(parents=True)
    (root / "entry.py").write_text(source, encoding="utf-8")
    (root / "helpers.py").write_text("value = 1\n", encoding="utf-8")
    return root


def test_both_pack_callers_use_the_one_checker() -> None:
    assert install.check_pack_imports is pack_imports.check_pack_imports
    assert loader.check_pack_imports is pack_imports.check_pack_imports


def test_check_pack_imports_accepts_local_modules(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example", "from .helpers import value\n")
    assert check_pack_imports(root, contract=ImportContract(), manifest=MANIFEST) == []


@pytest.mark.parametrize(
    "statement",
    ["from astrid.sdk._private import value\n", "from astrid.sdk import _private\n"],
)
def test_check_pack_imports_rejects_private_sdk_imports(tmp_path: Path, statement: str) -> None:
    root = _pack(tmp_path / "example", statement)
    contract = ImportContract(sdk_names=frozenset({"dto"}))
    violations = check_pack_imports(root, contract=contract, manifest=MANIFEST)
    assert violations and {v.code for v in violations} == {"deep_import"}


def test_check_pack_imports_checks_public_sdk_inventory(tmp_path: Path) -> None:
    root = _pack(tmp_path / "example", "from astrid.sdk.missing import value\n")
    contract = ImportContract(sdk_names=frozenset({"dto"}))
    violations = check_pack_imports(root, contract=contract, manifest=MANIFEST)
    assert [v.code for v in violations] == ["deep_import"]
    assert check_pack_imports(
        _pack(tmp_path / "ok", "from astrid.sdk.dto import value\n"),
        contract=contract, manifest=MANIFEST) == []
