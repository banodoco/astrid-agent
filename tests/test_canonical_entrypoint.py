from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from astrid.core.pack.entrypoint import canonical_runtime_entrypoint


def _write_guarded_module(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "from astrid.core.pack.entrypoint import guard_canonical_entrypoint",
                "guard_canonical_entrypoint('demo.capability')",
                "def main(argv=None):",
                "    return argv if argv is not None else 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_guard_blocks_direct_module_import_without_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRID_INTERNAL_INVOCATION", raising=False)
    module_path = tmp_path / "guarded_module.py"
    _write_guarded_module(module_path)

    with pytest.raises(SystemExit) as excinfo:
        _import_module(module_path, "guarded_module_direct")

    assert excinfo.value.code == 2


def test_canonical_runtime_entrypoint_allows_matching_capability_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRID_INTERNAL_INVOCATION", raising=False)
    module_path = tmp_path / "guarded_module.py"
    _write_guarded_module(module_path)

    with canonical_runtime_entrypoint("demo.capability"):
        module = _import_module(module_path, "guarded_module_allowed")

    assert module.main(["--ok"]) == ["--ok"]

    with canonical_runtime_entrypoint("other.capability"):
        with pytest.raises(SystemExit) as excinfo:
            _import_module(module_path, "guarded_module_wrong_capability")

    assert excinfo.value.code == 2


def test_shots_product_cli_imports_without_canonical_entrypoint_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested shots product parser retains canonical entrypoint behavior.

    Task T29 mounts ``astrid/packs/shots/cli.py`` beneath the timelines
    family. Product parser modules are imported by the gateway dispatch
    path and must stay importable without ``ASTRID_INTERNAL_INVOCATION``:
    ``guard_canonical_entrypoint`` remains reserved for pack ``run.py``
    modules, so importing and building the shots parser must never raise
    ``SystemExit`` (the guard's refusal) at import or build time.
    """
    monkeypatch.delenv("ASTRID_INTERNAL_INVOCATION", raising=False)
    from astrid.packs.shots import cli as shots_cli

    parser = shots_cli.build_parser(object())
    assert parser.prog == "astrid timelines shots"
    # The guard itself is still canonical and refuses direct module
    # invocation without runtime context (unchanged behavior).
    module_path = Path(__file__).parent / "guarded_module.py"
    _write_guarded_module(module_path)
    with pytest.raises(SystemExit) as excinfo:
        _import_module(module_path, "guarded_module_retained")
    assert excinfo.value.code == 2