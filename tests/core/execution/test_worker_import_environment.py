"""Worker import selection retains admitted pack custody across profiles."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import _worker_import_environment
from astrid.core.pack.identity import pack_digest
from runtime_protocol import platform


@pytest.mark.parametrize("kind", ["native", "command"])
def test_installed_builtin_capture_retains_verified_selection(tmp_path, monkeypatch, kind):
    import astrid.sdk.storage_root as storage_root
    import astrid.core.pack.imports as imports

    data = tmp_path / "data"
    area = data / "runtime" / "pack-captures"
    private = area / "capture"
    platform.private_directory(area)
    platform.private_directory(private)
    original = (Path(imports.__file__).resolve().parents[2] / "packs" / "rendering").resolve()
    snapshot = private / "rendering"
    shutil.copytree(original, snapshot)
    selected = {"rendering": {"root": str(snapshot), "source_digest": pack_digest(snapshot)}}
    relocations = {"rendering": {"original_root": str(original), **selected["rendering"]}}
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: data)

    result = _worker_import_environment(
        SimpleNamespace(), {"ASTRID_PACKS_PATH": "/ambient"}, kind=kind,
        selected=selected, relocations=relocations, installed=True,
    )

    assert result["ASTRID_PACKS_PATH"] == ""
    assert "PYTHONPATH" not in result
    assert json.loads(result["ASTRID_SELECTED_PACK_MAP"]) == selected
    assert json.loads(result["ASTRID_VERIFIED_PACK_RELOCATIONS"]) == relocations
    assert result["ASTRID_RUNTIME_DATA_ROOT"] == str(data)


def test_source_native_selected_map_replaces_host_roots_and_is_stable(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host

    selected_root = tmp_path / "selected" / "external"
    selected_root.mkdir(parents=True)
    (selected_root / "action.py").write_text("VALUE = 1\n")
    selected = {"external": {"root": str(selected_root), "source_digest": pack_digest(selected_root)}}
    monkeypatch.setattr(generic_host, "_dependency_pythonpath", lambda _environment: ())
    args = (SimpleNamespace(), {"ASTRID_PACKS_PATH": "/ambient"})
    kwargs = {"kind": "native", "pack_roots": (tmp_path / "host",),
              "python_roots": (), "selected": selected}

    validation = _worker_import_environment(*args, **kwargs)
    dispatch = _worker_import_environment(*args, **kwargs)

    assert validation == dispatch
    assert dispatch["ASTRID_PACKS_PATH"] == str(selected_root.parent)
    assert json.loads(dispatch["ASTRID_SELECTED_PACK_MAP"]) == selected
    assert str(tmp_path / "host") not in dispatch["ASTRID_PACKS_PATH"]
