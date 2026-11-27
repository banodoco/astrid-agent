#!/usr/bin/env python3
"""Exercise a real Backend.stage result with its own noneditable interpreter.

Run with <version>/py/bin/python -I this_file --version <version> --home
<isolated-home>. The version must be genuinely staged and not yet selected.
No sys.prefix, module, builder, transaction or loader substitution is used.
This destructive test owns only the explicitly supplied isolated home/version.
It does not start services or claim native/browser qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true", help="continue the retained first four passed cases")
    args = parser.parse_args()
    folder = args.version.absolute()
    assert Path(sys.prefix) == folder / "py", (sys.prefix, folder)
    if not args.resume:
        assert not (folder.parent.parent / "current").exists(), "use an unselected isolated stage"
        assert not (args.home / "Astrid/packs").exists(), "use an empty isolated home"
    from astrid.installer.core import Version, current, read_json, switch
    from astrid.installer.backend import Backend
    from astrid.installer import default_packs as delivery
    from astrid.core.pack import loader
    from astrid.core.pack.cli_manage import _validation_snapshot
    from astrid.core.pack.identity import pack_digest, read_pin, write_pin
    from astrid.core.pack.release_manifest import installed_release_manifest, installed_release_pins, verify_release_default
    from astrid.core.pack.transaction import install_group, read_group
    from astrid.installer._pack_cleanup import remove_pack_tree
    from astrid.packs.rendering.rendering.renderers.remotion.pack_revisions import resolved_pack_revisions

    for module in (delivery, loader):
        assert Path(module.__file__).is_relative_to(Path(sys.prefix))
    version = Version.load(read_json(folder / "version.json"))
    os.environ.update(version.env)
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home), ASTRID_NO_BROWSER="1")
    assert Path.home() == args.home
    manifest = installed_release_manifest(selected=False)[1]
    pin = manifest["packs"]["astrid_motion"]
    prepared = read_json(folder / "default-packs-prepared.json")
    compiler = prepared["proofs"]["astrid_motion"]["rendering"]
    assert len(compiler) == 20 and all(row["bytes"] > 0 and row["syntax_exit"] == 0 for row in compiler)
    root = args.home / "Astrid/packs"
    pack = root / "astrid_motion"
    definitions = pack / ".build/definitions.json"
    backend = Backend(folder.parent.parent, args.output.with_suffix(".commands.log"),
                      Path(version.env["ASTRID_INSTALL_UV"]))
    evidence = {"version": str(folder), "python": sys.version, "prefix": sys.prefix,
                "agent": manifest["agent"], "runtime": manifest["runtime"],
                "vibecomfy": manifest["vibecomfy"], "lifecycle": manifest["lifecycle"],
                "selected_source": pin, "compiler": compiler, "cases": []}

    def passed(name, **details):
        evidence["cases"].append({"name": name, "status": "PASS", **details})
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"case": name, "status": "PASS", **details}), flush=True)

    def interrupt(operation, phase):
        code = ("import os; from astrid.installer import default_packs as d; "
                "d." + operation + "(boundary=lambda phase: os._exit(73) if phase == " + repr(phase) + " else None)")
        result = subprocess.run([sys.executable, "-I", "-c", code],
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 73, result.stdout + result.stderr
        return read_group(root)

    if args.resume:
        evidence = read_json(args.output)
        assert evidence["version"] == str(folder) and evidence["selected_source"] == pin
        assert [row["name"] for row in evidence["cases"]][:4] == [
            "interruption-before-publication", "interruption-after-publication",
            "clean-installed-default-loader-and-source-identity", "environment-cannot-replace-selected-authority"]
        assert current(folder.parent.parent) == version
        if not any(row["name"] == "interrupted-rollback-and-retry" for row in evidence["cases"]):
            backend.apply_default_packs(version)
        after = read_group(root)
    else:
        before = interrupt("apply", "before_swap")
        assert before.revision == 0 and not before.paths
        assert current(folder.parent.parent) is None
        passed("interruption-before-publication", exit=73)
        after = interrupt("apply", "swapped")
        receipt = read_json(folder / "default-packs.json")
        assert after.transaction_id == receipt["publication"]["transaction_id"]
        assert current(folder.parent.parent) is None
        passed("interruption-after-publication", exit=73, transaction=after.transaction_id)
        backend.apply_default_packs(version)
        assert read_json(folder / "default-packs.json")["status"] == "applied"
        switch(folder.parent.parent, version)
        assert current(folder.parent.parent) == version
        observed = verify_release_default(pack, pin)
        assert observed["commit"] == pin["commit"] and pack_digest(pack) == pin["digest"]
        source_files = sorted(p for p in pack.rglob("*") if p.is_file()
                              and p.name != "PIN" and ".build" not in p.relative_to(pack).parts)
        assert len(source_files) == 64
        admitted = read_group(root).admissions["astrid_motion"]
        definitions = pack / ".build/definitions.json"
        assert hashlib.sha256(definitions.read_bytes()).hexdigest() == admitted["definitionsDigest"]
        scan = loader.scan_configured_packs()
        assert "astrid_motion" in {p.id for p in scan.packs}
        assert "astrid_motion" not in {p.pack_id for p in scan.quarantined}
        stage = {"effects": [{"source_pack_id": "astrid_motion",
                              "element_root": str(pack / "rendering/elements/effects/am-sprite")} ]}
        rows = resolved_pack_revisions(stage, {"astrid_motion": pin["commit"], "astrid_motion_tree": pin["digest"]})
        motion = next(row for row in rows if row["pack_id"] == "astrid_motion")
        assert motion["comparison"] == "match" and motion["upstream_revision"] == pin["commit"], motion
        passed("clean-installed-default-loader-and-source-identity", files=64, admission=admitted, comparison=motion)

        # The selected manifest wins over a caller's developer environment.
        os.environ["ASTRID_RELEASE_PACK_PINS"] = '{"astrid_motion":{"commit":"wrong","digest":"wrong"}}'
        assert dict(_validation_snapshot()[1]) == installed_release_pins()
        passed("environment-cannot-replace-selected-authority")
        os.environ.pop("ASTRID_RELEASE_PACK_PINS")

    # Rollback itself has the same durable interruption boundary. Its source
    # is the retained canonical generation, never a new download/copy path.
    if not any(row["name"] == "interrupted-rollback-and-retry" for row in evidence["cases"]):
        rollback_before = interrupt("rollback", "before_swap")
        assert rollback_before.transaction_id == after.transaction_id
        # The canonical rollback exposes a before-swap hook. Fail the real final
        # receipt write through filesystem permissions after the prepared receipt
        # is durable, rather than inventing a post-swap hook or mocking publication.
        mode = folder.stat().st_mode & 0o777
        code = ("import os; from pathlib import Path; from astrid.installer import default_packs as d;\n"
                "try:\n"
                " d.rollback(boundary=lambda phase: os.chmod(" + repr(str(folder)) + ", 0o500) if phase == 'before_swap' else None)\n"
                "except PermissionError:\n os._exit(73)\n")
        try:
            failed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=180)
            assert failed.returncode == 73, failed.stdout + failed.stderr
        finally:
            folder.chmod(mode)
        rollback_after = read_group(root)
        rollback_receipt = read_json(folder / "default-packs.json")["rollback"]
        assert rollback_after.transaction_id == rollback_receipt["transaction_id"]
        backend.rollback_default_packs(None, version)
        assert not read_group(root).paths
        assert current(folder.parent.parent) == version, "adapter must not select versions"
        passed("interrupted-rollback-and-retry", before_exit=73, after_exit=73)
    backend.apply_default_packs(version, version)
    assert backend.default_packs_need_repair(version) is False

    # A real user pack is admitted using the original folder transaction.
    user = args.home / "user-source"
    user.mkdir(exist_ok=True)
    (user / "pack.yaml").write_text('schema_version: 3\nid: user_fixture\nname: User fixture\nversion: 1.0.0\npack_api: "2.0"\ndocumentation: {kind: none, reason: Proof fixture.}\n')
    (user / "notes.txt").write_bytes(b"user pack data\x00preserved\n")
    if not (root / "user_fixture").exists():
        install_group(source=user, packs={"user_fixture": "."}, packs_root=root,
                      python_packages=delivery._packages(), origin="folder")
    user_bytes = {p.relative_to(root / "user_fixture").as_posix(): p.read_bytes()
                  for p in (root / "user_fixture").rglob("*") if p.is_file()}
    protected = args.home / "protected-user-file"
    protected.write_bytes(b"keep this user document\n")
    for damaged in ("definitions", "source", "pin"):
        if any(row["name"] == "quarantine-and-repair-" + damaged for row in evidence["cases"]):
            continue
        if damaged == "definitions":
            definitions.write_text("{}")
        elif damaged == "source":
            (pack / "README.md").write_text("damaged source")
        else:
            altered = read_pin(pack / "PIN")
            altered["commit"] = "0" * 40
            write_pin(pack / "PIN", altered)
        rejected = loader.scan_configured_packs()
        assert "astrid_motion" not in {p.id for p in rejected.packs}
        assert "astrid_motion" in {p.pack_id for p in rejected.quarantined}
        assert backend.default_packs_need_repair(version) is True
        backend.apply_default_packs(version, version)
        verify_release_default(pack, pin)
        passed("quarantine-and-repair-" + damaged)
    remove_pack_tree(pack)
    assert backend.default_packs_need_repair(version) is True
    backend.apply_default_packs(version, version)
    verify_release_default(pack, pin)
    assert protected.read_bytes() == b"keep this user document\n"
    assert user_bytes == {p.relative_to(root / "user_fixture").as_posix(): p.read_bytes()
                          for p in (root / "user_fixture").rglob("*") if p.is_file()}
    passed("missing-default-repair-preserves-user-state")

    install_group(source="", packs={}, packs_root=root, python_packages=delivery._packages(), remove=("astrid_motion",))
    removed = read_group(root)
    assert removed.removals == ("astrid_motion",)
    assert backend.default_packs_need_repair(version) is False
    backend.apply_default_packs(version, version)
    assert read_group(root).transaction_id == removed.transaction_id and not pack.exists()
    assert protected.read_bytes() == b"keep this user document\n"
    assert current(folder.parent.parent) == version
    passed("explicit-removal-survives-repair")
    evidence["result"] = "PASS"
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
