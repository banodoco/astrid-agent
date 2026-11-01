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
