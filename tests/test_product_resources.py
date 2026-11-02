from pathlib import Path

import pytest

from astrid.core import product_resources


@pytest.mark.parametrize("name,source_name", [
    ("runtime.lock.json", "runtime.lock.json"),
    ("astrid-beta-capabilities.json", "config/astrid-beta-capabilities.json"),
])
def test_resource_layouts_and_release_data(tmp_path, monkeypatch, name, source_name):
    root = Path(__file__).resolve().parents[1]
    assert (root / "astrid/resources" / name).read_bytes() == (root / source_name).read_bytes()
    package = tmp_path / "package"
    package.mkdir()
    monkeypatch.setattr(product_resources, "_PACKAGE_RESOURCES", package)
    installed = tmp_path / "installed" / name
    source = tmp_path / "source" / name
    source.parent.mkdir()
    source.write_text("source")
    resolve = lambda: product_resources.product_resource(name, installed=installed, source=source)
    assert resolve() == source
    (package / name).write_text("packaged")
    assert resolve() == package / name
    installed.parent.mkdir()
    installed.write_text("installer")
    assert resolve() == installed
    installed.unlink()
    (package / name).unlink()
    source.unlink()
    with pytest.raises(FileNotFoundError, match="reinstall or update"):
        resolve()


def test_generic_host_selects_matrix_only_for_its_own_installed_packs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host

    root = tmp_path / "site-packages"
    module = root / "astrid/core/execution/generic_host.py"
    module.parent.mkdir(parents=True)
    module.touch()
    matrix = root / "astrid/resources/astrid-beta-capabilities.json"
    matrix.parent.mkdir(parents=True)
    matrix.write_text("{}")
    monkeypatch.setattr(generic_host, "__file__", str(module))
    assert generic_host.GenericPackHost._default_matrix_path(
        SimpleNamespace(pack_roots=[root / "astrid/packs"])) == matrix
    assert generic_host.GenericPackHost._default_matrix_path(
        SimpleNamespace(pack_roots=[tmp_path / "other"])) is None
    matrix.unlink()
    with pytest.raises(generic_host.HostError, match="reinstall or update"):
        generic_host.GenericPackHost._default_matrix_path(
            SimpleNamespace(pack_roots=[root / "astrid/packs"]))
