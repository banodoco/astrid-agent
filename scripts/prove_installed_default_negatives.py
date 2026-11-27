#!/usr/bin/env python3
"""Negative inputs in an actual, stopped, selected installed proof venue.

Run with the selected version's real interpreter and -I. This program mutates
only the explicitly supplied isolated proof installation, restores every input,
and verifies that failed repair never changes its collection or selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home))
    from astrid.core.pack.install import PackInstallError
    from astrid.core.pack.loader import scan_configured_packs
    from astrid.core.pack.release_manifest import installed_release_manifest, verify_release_default
    from astrid.installer import default_packs as delivery
    from astrid.installer.core import Version, current, read_json

    folder, manifest = installed_release_manifest()
    assert Path(sys.prefix) == folder / "py"
    assert Path(delivery.__file__).is_relative_to(Path(sys.prefix))
    version = Version.load(read_json(folder / "version.json"))
    os.environ.update(version.env)
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home))
    program = folder.parent.parent
    assert current(program) == version
    # The caller stops the actual Runtime through its existing Backend fence.
    support = Path(version.env["ASTRID_RUNTIME_DATA_ROOT"]) / "runtime"
    assert not (support / "discovery.json").exists(), "stop the proof Runtime first"
    collection = args.home / "Astrid/packs"
    pack = collection / "astrid_motion"
    verify_release_default(pack, manifest["packs"]["astrid_motion"])
    pointer = (program / "current").read_bytes()

    def snapshot():
        return {p.relative_to(collection).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in collection.rglob("*") if p.is_file()}

    before = snapshot()
    evidence = {"prefix": sys.prefix, "agent": manifest["agent"],
                "source": manifest["packs"]["astrid_motion"], "cases": []}

    def preserved():
        assert current(program) == version
        assert (program / "current").read_bytes() == pointer
        assert snapshot() == before

    def passed(name):
        evidence["cases"].append({"name": name, "status": "PASS"})
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps(evidence["cases"][-1]), flush=True)

    path = folder / "manifest.json"
    raw = path.read_bytes()
    for kind in ("wrong-commit", "wrong-digest", "duplicate-json-key"):
        try:
            value = json.loads(raw)
            if kind == "wrong-commit":
                value["packs"]["astrid_motion"]["commit"] = "0" * 40
            elif kind == "wrong-digest":
                value["packs"]["astrid_motion"]["digest"] = "0" * 64
            if kind == "duplicate-json-key":
                path.write_bytes(b'{"packs":{},' + raw.lstrip()[1:])
            else:
                path.write_text(json.dumps(value))
            try:
                delivery.apply(previous=folder)
            except PackInstallError:
                pass
            else:
                raise AssertionError("Changed selected input was admitted: " + kind)
            assert (program / "current").read_bytes() == pointer and snapshot() == before
        finally:
            path.write_bytes(raw)
        preserved()
        passed(kind + "-refuses-without-mutation")

    retained = folder / "manifest.negative-proof-retained.json"
    assert not retained.exists()
    try:
        path.rename(retained)
        path.symlink_to(retained)
        try:
            installed_release_manifest()
        except PackInstallError:
            pass
        else:
            raise AssertionError("Aliased selected manifest was admitted")
    finally:
        path.unlink(missing_ok=True)
        retained.rename(path)
    preserved()
    passed("aliased-selected-manifest-refuses")

    prepared = folder / "default-packs-prepared.json"
    kept = folder / "prepared.negative-proof-retained.json"
    assert not kept.exists()
    try:
        prepared.rename(kept)
        try:
            delivery.needs_repair()
        except PackInstallError:
            pass
        else:
            raise AssertionError("Missing preparation disabled delivery")
    finally:
        kept.rename(prepared)
    preserved()
    passed("missing-preparation-receipt-refuses")

    group_path = collection / ".group.json"
    group_bytes = group_path.read_bytes()
    try:
        group = json.loads(group_bytes)
        del group["admissions"]["astrid_motion"]
        group_path.write_text(json.dumps(group))
        scan = scan_configured_packs()
        assert "astrid_motion" not in {item.id for item in scan.packs}
        assert "astrid_motion" in {item.pack_id for item in scan.quarantined}
    finally:
        group_path.write_bytes(group_bytes)
    preserved()
    passed("missing-canonical-admission-quarantines")

    from astrid.core.pack.identity import read_pin, write_pin
    pin_path = pack / "PIN"
    pin_bytes = pin_path.read_bytes()
    try:
        value = read_pin(pin_path)
        value["origin"] = "user"
        write_pin(pin_path, value)
        conflicting = snapshot()
        try:
            delivery.apply(previous=folder)
        except PackInstallError:
            pass
        else:
            raise AssertionError("Repair took over user-origin input")
        assert snapshot() == conflicting
    finally:
        pin_path.write_bytes(pin_bytes)
    preserved()
    passed("user-origin-conflict-refuses-without-takeover")
    verify_release_default(pack, manifest["packs"]["astrid_motion"])
    evidence["result"] = "PASS"
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
