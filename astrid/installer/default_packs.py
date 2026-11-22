"""Install release defaults in the staged Agent's Python environment."""
from __future__ import annotations

import sys

from astrid.core.pack.cli_manage import (
    _build_inputs, _composition_roots, _core_root, _selected_root, _validation_snapshot,
)
from astrid.core.pack.defaults import _default_pins, install_defaults, load_release_manifest


def main(manifest_path: str) -> None:
    manifest = load_release_manifest(manifest_path)
    source, revision, packs, _ = _default_pins(manifest)
    if not packs:
        return
    root = _selected_root(None)
    packages, pins = _validation_snapshot()
    builds, toolchain = _build_inputs(source, revision, packs, None)
    install_defaults(manifest, packs_root=root, python_packages=packages,
                     selected_roots=_composition_roots(root, include_default=True),
                     core_root=_core_root(), release_pins=pins,
                     builds=builds, toolchain=toolchain)


if __name__ == "__main__":
    main(sys.argv[1])
