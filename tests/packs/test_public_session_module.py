"""M2-11a Luna H: the public pack-facing session module stays narrow."""
from __future__ import annotations

import ast
import types
from pathlib import Path

import astrid.packs.session as public_session
from astrid.core.execution import session_client

MODULE_PATH = Path(public_session.__file__)


def test_exports_are_exactly_the_public_names():
    assert sorted(public_session.__all__) == ["SessionChannelError", "SessionClientProtocol"]
    public_bindings = {
        name for name, value in vars(public_session).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
    }
    assert public_bindings == {"SessionChannelError", "SessionClientProtocol"}


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
