"""Focused tests for import-origin matching in the pack API codemod."""

from astrid import pack_api
from scripts.codemod_pack_api import _is_export_from


def test_same_name_export_is_converted_only_from_its_defining_module() -> None:
    export_origins = pack_api._EXPORTS

    assert export_origins["SCHEMA_VERSION"] == "astrid.core.contracts.runtime_orchestration"
    assert _is_export_from(
        "astrid.core.contracts.runtime_orchestration", "SCHEMA_VERSION", export_origins
    )
    assert not _is_export_from(
        "astrid.core.rendering.contracts", "SCHEMA_VERSION", export_origins
    )
