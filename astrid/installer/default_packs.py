"""Install release defaults in the staged Agent's Python environment."""
from __future__ import annotations

import contextlib
import json
import sys

from astrid.core.pack.cli_manage import (
    _build_inputs, _composition_roots, _core_root, _selected_root, _validation_snapshot,
)
from astrid.core.pack.defaults import _default_pins, install_defaults, load_release_manifest
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.transaction import GroupCommitError


def _install(manifest_path: str):
    manifest = load_release_manifest(manifest_path)
    source, revision, packs, _ = _default_pins(manifest)
    if not packs:
        return
    root = _selected_root(None)
    packages, pins = _validation_snapshot()
    builds, toolchain = _build_inputs(source, revision, packs, None)
    return install_defaults(manifest, packs_root=root, python_packages=packages,
                            selected_roots=_composition_roots(root, include_default=True),
                            core_root=_core_root(), release_pins=pins,
                            builds=builds, toolchain=toolchain)


def main(manifest_path: str) -> None:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = _install(manifest_path)
    except GroupCommitError as exc:
        report = {"status": "recovery-needed", "transaction_id": exc.transaction_id,
                  "revision": exc.revision, "recovery": str(exc)}
        print(json.dumps(report), flush=True)
        raise
    except PackInstallError:
        print(json.dumps({"status": "rejected", "transaction_id": None,
                          "revision": None, "recovery": None}), flush=True)
        raise
    report = {"status": "published" if result and result.changed_ids else "no-change",
              "transaction_id": result.transaction_id if result else None,
              "revision": result.revision if result else None, "recovery": None}
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
