#!/usr/bin/env python3
"""Prove rollback compatibility with an actual earlier installed group reader."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-python", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expect-legacy-refusal", action="store_true")
    args = parser.parse_args()
    args.home.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home),
                      ASTRID_HOME=str(args.home / ".astrid"), XDG_STATE_HOME=str(args.home / "state"),
                      XDG_DATA_HOME=str(args.home / "data"),
                      ASTRID_WORKSPACE_CONFIG_DIR=str(args.home / "config"))
    from astrid.core.pack.release_manifest import installed_release_manifest, install_release_manifest, release_pins
    from astrid.core.pack.transaction import read_group, rollback_group
    from astrid.installer.default_packs import _packages
    folder, selected = installed_release_manifest()
    assert Path(sys.prefix) == folder / "py"
    pointer = (folder.parent.parent / "current").read_bytes()
    root = args.home / "Astrid/packs"
    old_install = """import hashlib,json,sys
from pathlib import Path
from astrid.core.pack import transaction
from astrid.core.pack.release_manifest import installed_release_manifest,install_release_manifest
from astrid.installer.default_packs import _packages
folder,manifest=installed_release_manifest();assert Path(sys.prefix)==folder/'py'
assert Path(transaction.__file__).is_relative_to(Path(sys.prefix))
root=Path.home()/'Astrid/packs'
install_release_manifest(manifest=manifest,packs_root=root,python_packages=_packages())
assert 'pins' not in json.loads((root/'.group.json').read_bytes())
(root/'protected-user.bin').write_bytes(b'preserve old user bytes\\x00\\xff')
print(json.dumps({'prefix':sys.prefix,'manifest':manifest,'group':json.loads((root/'.group.json').read_bytes()),
                 'transaction_sha256':hashlib.sha256(Path(transaction.__file__).read_bytes()).hexdigest()}))
"""
    ran = subprocess.run([str(args.legacy_python), "-B", "-I", "-c", old_install],
                         capture_output=True, text=True, timeout=180)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    earlier = json.loads(ran.stdout)
    first = read_group(root)
    before_source = {p.relative_to(root / "astrid_motion").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (root / "astrid_motion").rglob("*") if p.is_file()}
    user = (root / "protected-user.bin").read_bytes()
    records = []
    current = install_release_manifest(manifest=selected, packs_root=root, python_packages=_packages(),
                                       on_prepared=records.append)
    record, = records
    assert read_group(root).pins["astrid_motion"]["origin"] == "default"
    rollback_group(packs_root=root, transaction_id=current.transaction_id, pack_ids=record["pack_ids"],
                   expected_pins=record["pins"], expected_previous=(first.transaction_id, first.revision),
                   release_pins=release_pins(earlier["manifest"]), python_packages=_packages())
    assert (root / "protected-user.bin").read_bytes() == user
    assert before_source == {p.relative_to(root / "astrid_motion").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (root / "astrid_motion").rglob("*") if p.is_file()}
    old_read = """import json,sys
from pathlib import Path
from astrid.core.pack.transaction import read_group
from astrid.core.pack.loader import scan_configured_packs
from astrid.core.pack.release_manifest import installed_release_manifest,verify_release_default
folder,manifest=installed_release_manifest();assert Path(sys.prefix)==folder/'py'
root=Path.home()/'Astrid/packs';group=read_group(root)
verify_release_default(root/'astrid_motion',manifest['packs']['astrid_motion'])
scan=scan_configured_packs();assert 'astrid_motion' in {p.id for p in scan.packs}
print(json.dumps({'prefix':sys.prefix,'transaction_id':group.transaction_id,'revision':group.revision,'admitted':True}))
"""
    read = subprocess.run([str(args.legacy_python), "-B", "-I", "-c", old_read],
                          capture_output=True, text=True, timeout=180)
    result = {"new_prefix": sys.prefix, "new_agent": selected["agent"], "new_source": selected["packs"],
              "old": earlier, "publication": record, "rollback_group": json.loads((root / ".group.json").read_bytes()),
              "legacy_read": {"exit": read.returncode, "stdout": read.stdout, "stderr": read.stderr},
              "source_files_restored": len(before_source), "user_bytes_preserved": True,
              "result": "COUNTEREXAMPLE" if read.returncode else "PASS"}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    assert (folder.parent.parent / "current").read_bytes() == pointer
    if args.expect_legacy_refusal:
        assert read.returncode != 0 and "Group state is malformed" in read.stderr
    else:
        assert read.returncode == 0, read.stdout + read.stderr
    print(json.dumps({"result": result["result"], "old_reader_exit": read.returncode}), flush=True)


if __name__ == "__main__":
    main()
