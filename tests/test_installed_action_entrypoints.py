"""Every packaged action runner must import from the installed wheel."""

from __future__ import annotations

import importlib
from pathlib import Path
import sysconfig

import pytest


@pytest.mark.installed
def test_all_packaged_action_entrypoints_import_from_installed_wheel() -> None:
    import astrid

    package_root = Path(next(iter(astrid.__path__))).resolve()
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    assert package_root.is_relative_to(purelib), f"Astrid imported outside site-packages: {package_root}"
    source_pack_root = Path(__file__).resolve().parents[1] / "astrid" / "packs"
    installed_pack_root = package_root / "packs"
    source_entrypoints = {
        path.relative_to(source_pack_root)
        for path in source_pack_root.glob("*/actions/**/*.py")
        if path.name in {"run.py", "entrypoint.py"}
    }
    installed_entrypoints = {
        path.relative_to(installed_pack_root)
        for path in installed_pack_root.glob("*/actions/**/*.py")
        if path.name in {"run.py", "entrypoint.py"}
    }
    assert installed_entrypoints == source_entrypoints, (
        f"wheel action entrypoints differ from checkout; missing={sorted(source_entrypoints - installed_entrypoints)}, "
        f"unexpected={sorted(installed_entrypoints - source_entrypoints)}"
    )
    entrypoint_files = sorted(
        path
        for path in installed_pack_root.glob("*/actions/**/*.py")
        if path.name in {"run.py", "entrypoint.py"}
    )
    assert entrypoint_files, "no packaged action entrypoints were found"

    imported = []
    for path in entrypoint_files:
        module_name = ".".join(path.relative_to(package_root.parent).with_suffix("").parts)
        module = importlib.import_module(module_name)
        assert Path(module.__file__).resolve() == path.resolve(), module_name
        imported.append(module_name)

    h3_entrypoints = [name for name in imported if ".packs.h3_av.actions." in name]
    assert h3_entrypoints, "H3 action entrypoints were not imported"
