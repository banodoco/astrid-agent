"""M2-11a: the pack-facing session names are reached only through astrid.sdk.

The names live in ``astrid/sdk/session.py`` and are exported from
``astrid.sdk``. A pack that imports them from ``astrid.sdk.session`` or from the
retired ``astrid.packs.session`` path is a deep import and is refused by the
pack import policy; the sanctioned ``from astrid.sdk import ...`` is accepted.
"""
from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

import astrid.sdk as sdk
import astrid.sdk.session as public_session
from astrid.core.execution import session_client
from astrid.pack_imports import ImportContract, check_pack_imports

MODULE_PATH = Path(public_session.__file__)
PUBLIC_NAMES = {"SessionChannelError", "SessionClientProtocol"}


def test_exports_are_exactly_the_public_names():
    assert sorted(public_session.__all__) == sorted(PUBLIC_NAMES)
    public_bindings = {
        name for name, value in vars(public_session).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
    }
    assert public_bindings == PUBLIC_NAMES


def test_sdk_exports_the_session_names_lazily():
    assert PUBLIC_NAMES <= set(sdk.__all__)
    assert sdk.SessionChannelError is session_client.SessionChannelError
    assert sdk.SessionClientProtocol is public_session.SessionClientProtocol


def test_session_channel_error_is_the_same_class_not_a_copy():
    assert public_session.SessionChannelError is session_client.SessionChannelError


def test_module_imports_no_core_internals_beyond_the_reexport():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, alias.asname) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed in the public session module"
            imports.extend((f"{node.module}.{alias.name}", alias.asname) for alias in node.names)
    assert sorted(imports) == sorted([
        ("typing", None),
        ("astrid.core.execution.session_client.SessionChannelError", None),
    ])


def test_retired_astrid_packs_session_path_is_gone():
    with pytest.raises(ModuleNotFoundError):
        __import__("astrid.packs.session")


_CONTRACT = ImportContract(sdk_names=frozenset(sdk.__all__))
_MANIFEST = {"id": "sessionprobe"}


def _codes(tmp_path, source: str) -> list[tuple[str, str]]:
    pack_dir = tmp_path / "sessionprobe"
    pack_dir.mkdir()
    (pack_dir / "action.py").write_text(source, encoding="utf-8")
    violations = check_pack_imports(pack_dir, contract=_CONTRACT, manifest=_MANIFEST)
    return [(v.module, v.code) for v in violations]


def test_pack_may_import_session_names_from_astrid_sdk(tmp_path):
    assert _codes(tmp_path, "from astrid.sdk import SessionChannelError, SessionClientProtocol\n") == []


def test_pack_deep_import_of_sdk_session_module_is_refused(tmp_path):
    violations = _codes(tmp_path, "from astrid.sdk.session import SessionChannelError\n")
    assert violations and all(code == "deep_import" for _, code in violations)


def test_pack_import_of_retired_packs_session_path_is_refused(tmp_path):
    violations = _codes(tmp_path, "from astrid.packs.session import SessionChannelError\n")
    # Refused as a non-public core import (undeclared pack), never accepted.
    assert violations and all(code != "" for _, code in violations)
    assert all(module.startswith("astrid.packs.session") for module, _ in violations)
