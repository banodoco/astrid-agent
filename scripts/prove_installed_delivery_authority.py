#!/usr/bin/env python3
"""P1 regression proof in a genuine selected noneditable installation.

Use its own interpreter with -I. Each case owns a fresh HOME/collection; the
selected version, its manifest, and the public-install HOME are never edited.
All pack acquisition/publication/rollback uses the existing canonical group
transaction. No prefix, loader, builder, publication, or module substitution.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--legacy-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home))
    from astrid.core.pack import loader, release_manifest, transaction
    from astrid.core.pack.identity import pack_digest, read_pin, write_pin
    from astrid.core.pack.install import PackInstallError
    from astrid.installer import default_packs as delivery
    from astrid.installer.core import Version, current, read_json
    from astrid.packs.rendering.rendering.renderers.remotion.pack_revisions import resolved_pack_revisions

    folder, manifest = release_manifest.installed_release_manifest()
    version = Version.load(read_json(folder / "version.json"))
    assert Path(sys.prefix) == folder / "py" and current(folder.parent.parent) == version
    assert manifest["agent"]["sha"] == args.source_commit
    os.environ.update(version.env)
    os.environ.update(HOME=str(args.home), USERPROFILE=str(args.home))
    assert not (Path(version.env["ASTRID_RUNTIME_DATA_ROOT"]) / "runtime/discovery.json").exists()
    pointer = (folder.parent.parent / "current").read_bytes()
    manifest_raw = (folder / "manifest.json").read_bytes()
    modules = {}
    for module in (loader, release_manifest, transaction, delivery):
        path = Path(module.__file__)
        assert path.is_relative_to(Path(sys.prefix))
        relative = Path(*path.parts[path.parts.index("astrid"):]).as_posix()
        expected = subprocess.check_output(["git", "-C", str(args.source_checkout), "show",
                                            args.source_commit + ":" + relative])
        assert path.read_bytes() == expected, relative
        modules[relative] = {"installed_path": str(path), "sha256": hashlib.sha256(expected).hexdigest()}
    selected_pin = manifest["packs"]["astrid_motion"]
    original_home = args.home

    def files(root):
        return {p.relative_to(root).as_posix():
                {"kind": "link", "target": os.readlink(p)} if p.is_symlink() else
                {"kind": "directory"} if p.is_dir() else
                {"kind": "file", "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
                for p in sorted(root.rglob("*"))}

    public_bytes = files(original_home / "Astrid/packs")
    evidence = {"source_commit": args.source_commit, "manifest_agent": manifest["agent"],
                "pack_source": selected_pin, "prefix": sys.prefix, "python": sys.version,
                "module_matches": modules, "cases": []}

    def preserved_selection():
        assert (folder.parent.parent / "current").read_bytes() == pointer
        assert (folder / "manifest.json").read_bytes() == manifest_raw
        assert current(folder.parent.parent) == version
        assert files(original_home / "Astrid/packs") == public_bytes

    def passed(name, **details):
        preserved_selection()
        evidence["cases"].append({"name": name, "status": "PASS", **details})
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps(evidence["cases"][-1]), flush=True)

    @contextmanager
    def isolated(name):
        home = args.cases / name
        home.mkdir(parents=True, mode=0o700, exist_ok=False)
        env = dict(os.environ)
        os.environ.update(HOME=str(home), USERPROFILE=str(home), ASTRID_HOME=str(home / ".astrid"),
                          XDG_STATE_HOME=str(home / "state"), XDG_DATA_HOME=str(home / "data"),
                          ASTRID_RUNTIME_DATA_ROOT=str(home / "data"),
                          ASTRID_WORKSPACE_CONFIG_DIR=str(home / "config"))
        for key in ("ASTRID_PACKS_PATH", "ASTRID_PACK_SOURCES_JSON", "ASTRID_RELEASE_MANIFEST", "ASTRID_RELEASE_PACK_PINS"):
            os.environ.pop(key, None)
        try:
            yield home / "Astrid/packs"
        finally:
            os.environ.clear()
            os.environ.update(env)

    def observed():
        scan = loader.scan_configured_packs()
        return {"admitted": "astrid_motion" in {p.id for p in scan.packs},
                "quarantine": [p.error for p in scan.quarantined if p.pack_id == "astrid_motion"]}

    def install(root, *, origin="default"):
        return release_manifest.install_release_manifest(manifest=manifest, packs_root=root,
                                                         origin=origin, python_packages=delivery._packages())

    def removed(root, *, tombstone=False):
        first = install(root)
        records = []
        if tombstone:
            result = transaction.install_group(source="", packs={}, packs_root=root,
                python_packages=delivery._packages(), remove=("astrid_motion",), on_prepared=records.append)
        else:
            result = release_manifest.install_release_manifest(manifest={"packs": {}}, packs_root=root,
                python_packages=delivery._packages(), on_prepared=records.append)
        record, = records
        assert not (root / "astrid_motion").exists() and record["pins"]["astrid_motion"] is None
        return record, dict(packs_root=root, transaction_id=result.transaction_id,
            pack_ids=record["pack_ids"], expected_pins=record["pins"],
            expected_previous=(first.transaction_id, first.revision),
            release_pins=release_manifest.release_pins(manifest), python_packages=delivery._packages())

    def public_new(root):
        argv = [sys.executable, "-B", "-I", "-m", "astrid.core.pack.cli", "new", "astrid_motion",
                "--destination", str(root / "astrid_motion"), "--role", "action"]
        ran = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert not (root / "astrid_motion/PIN").exists()
        return {"argv": argv, "exit": ran.returncode, "stdout": ran.stdout, "stderr": ran.stderr}

    def sentinel(root):
        with transaction.collection_write_custody(root):
            (root / "astrid_motion/user-sentinel.bin").write_bytes(b"all user bytes survive\x00\xff\n")

    def refuse(root, request, **kwargs):
        before = files(root)
        state = (root / ".group.json").read_bytes()
        try:
            transaction.rollback_group(**request, **kwargs)
        except PackInstallError as exc:
            assert exc.code == "rollback_conflict", (exc.code, str(exc))
            reason = str(exc)
        else:
            raise AssertionError("Rollback overwrote a recreated named object")
        assert files(root) == before
        assert (root / ".group.json").read_bytes() == state
        return {"code": "rollback_conflict", "reason": reason, "preserved_files": before}

    try:
        with isolated("loader") as root:
            installed, compiled = delivery._install(manifest, root)
            pack = root / "astrid_motion"
            assert len(compiled["astrid_motion"]["rendering"]) == 20
            assert transaction.read_group(root).pins["astrid_motion"] == read_pin(pack / "PIN")
            assert observed() == {"admitted": True, "quarantine": []}
            rows = resolved_pack_revisions({"effects": [{"source_pack_id": "astrid_motion",
                "element_root": str(pack / "rendering/elements/effects/am-sprite")}]},
                {"astrid_motion": selected_pin["commit"], "astrid_motion_tree": selected_pin["digest"]})
            assert next(r for r in rows if r["pack_id"] == "astrid_motion")["comparison"] == "match"
            passed("canonical-clean-loader-and-current-witness", transaction=installed.transaction_id,
                   compiler=compiled["astrid_motion"]["rendering"], comparison=rows)
            source = pack / "rendering/elements/effects/am-sprite/component.tsx"
            original, pin_raw = source.read_bytes(), (pack / "PIN").read_bytes()
            group_raw = (root / ".group.json").read_bytes()
            for changed in (False, True):
                for damage in ("missing", "user", "folder"):
                    try:
                        with transaction.collection_write_custody(root):
                            if changed:
                                source.write_bytes(original + b"\n// user source mutation\n")
                            if damage == "missing":
                                (pack / "PIN").unlink()
                            else:
                                pin = read_pin(pack / "PIN")
                                pin["origin"] = damage
                                if damage == "folder":
                                    pin.update(source=pack.as_uri(), commit=None, digest=None)
                                write_pin(pack / "PIN", pin)
                        before = files(root)
                        result = observed()
                        assert not result["admitted"] and result["quarantine"], result
                        assert files(root) == before and (root / ".group.json").read_bytes() == group_raw
                        passed("loader-" + damage + ("-changed-source" if changed else "-matching-source"),
                               observed=result, digest=pack_digest(pack), user_bytes_preserved=True)
                    finally:
                        with transaction.collection_write_custody(root):
                            source.write_bytes(original)
                            (pack / "PIN").write_bytes(pin_raw)
            assert observed() == {"admitted": True, "quarantine": []}

        for legacy in (False, True):
            for origin in ("user", "folder"):
                name = ("legacy-" if legacy else "bound-") + origin
                with isolated(name) as root:
                    checkout = root.parent.parent / "source"
                    if origin == "folder":
                        from urllib.parse import urlsplit, unquote
                        mirror = Path(unquote(urlsplit(selected_pin["repo"]).path))
                        command = ["git", "-C", str(mirror), "worktree", "add", "--detach", str(checkout), selected_pin["commit"]]
                        ran = subprocess.run(command, capture_output=True, text=True, timeout=60)
                        assert ran.returncode == 0, ran.stdout + ran.stderr
                    if legacy:
                        data = root.parent.parent / "legacy-request.json"
                        data.write_text(json.dumps({"manifest": manifest, "root": str(root),
                                                   "origin": origin, "source": str(checkout)}))
                        code = """import hashlib,json,sys
from pathlib import Path
from astrid.core.pack import transaction
from astrid.core.pack.release_manifest import install_release_manifest
from astrid.installer.default_packs import _packages
r=json.loads(Path(sys.argv[1]).read_text())
assert Path(transaction.__file__).is_relative_to(Path(sys.prefix))
if r['origin']=='user':
 install_release_manifest(manifest=r['manifest'],packs_root=r['root'],origin='user',python_packages=_packages())
else:
 transaction.install_group(source=r['source'],packs={'astrid_motion':'astrid_motion'},packs_root=r['root'],origin='folder',python_packages=_packages())
assert 'pins' not in json.loads((Path(r['root'])/'.group.json').read_bytes())
print(json.dumps({'prefix':sys.prefix,'transaction_module':transaction.__file__,'transaction_sha256':hashlib.sha256(Path(transaction.__file__).read_bytes()).hexdigest()}))
"""
                        ran = subprocess.run([str(args.legacy_python), "-B", "-I", "-c", code, str(data)],
                                             capture_output=True, text=True, timeout=180)
                        assert ran.returncode == 0, ran.stdout + ran.stderr
                        writer = json.loads(ran.stdout)
                    elif origin == "user":
                        install(root, origin="user")
                        writer = {"prefix": sys.prefix}
                    else:
                        transaction.install_group(source=checkout, packs={"astrid_motion": "astrid_motion"},
                            packs_root=root, python_packages=delivery._packages(), origin="folder")
                        writer = {"prefix": sys.prefix}
                    pack = root / "astrid_motion"
                    assert read_pin(pack / "PIN")["origin"] == origin
                    for changed in (False, True):
                        if changed:
                            with transaction.collection_write_custody(root):
                                source = pack / "rendering/elements/effects/am-sprite/component.tsx"
                                source.write_bytes(source.read_bytes() + b"\n// intentional user edit\n")
                        before = files(root)
                        assert observed() == {"admitted": True, "quarantine": []}
                        assert files(root) == before
                        passed(name + ("-edited-source" if changed else "-matching-source"),
                               writer=writer, user_bytes_preserved=True)

        for kind in ("public-new", "unpinned-directory", "file", "empty-directory", "dangling-link"):
            with isolated("rollback-" + kind) as root:
                record, request = removed(root)
                command = public_new(root) if kind == "public-new" else None
                target = root / "astrid_motion"
                with transaction.collection_write_custody(root):
                    if kind in {"empty-directory", "unpinned-directory"}:
                        target.mkdir()
                    elif kind == "file":
                        target.write_bytes(b"user file at the removed path\x00\xff")
                    elif kind == "dangling-link":
                        target.symlink_to(root.parent / "user-selected-absent-target")
                if kind in {"public-new", "unpinned-directory"}:
                    sentinel(root)
                result = refuse(root, request)
                passed("rollback-preserves-" + kind, publication=record, authoring=command, **result)

        with isolated("rollback-interrupted") as root:
            record, request = removed(root)
            payload = root.parent.parent / "rollback-input.json"
            serializable = {**request, "packs_root": str(root)}
            payload.write_text(json.dumps(serializable))
            prepared = root.parent.parent / "rollback-prepared.json"
            code = """import json,os,sys
from pathlib import Path
from astrid.core.pack.transaction import rollback_group
from astrid.installer.core import write_json
r=json.loads(Path(sys.argv[1]).read_text());r['expected_previous']=tuple(r['expected_previous'])
def prepared(value):
 write_json(Path(sys.argv[2]),value)
 os._exit(73)
rollback_group(**r,on_prepared=prepared)
"""
            ran = subprocess.run([sys.executable, "-B", "-I", "-c", code, str(payload), str(prepared)],
                                 capture_output=True, text=True, timeout=180)
            assert ran.returncode == 73 and prepared.is_file(), ran.stdout + ran.stderr
            assert transaction.read_group(root).transaction_id == record["transaction_id"]
            command = public_new(root)
            sentinel(root)
            passed("interrupted-rollback-retry-preserves-public-user-pack", exit=ran.returncode,
                   prepared=json.loads(prepared.read_bytes()), authoring=command, **refuse(root, request))

        with isolated("rollback-prepared-race") as root:
            _record, request = removed(root)
            written = []
            def on_prepared(_receipt):
                with transaction.collection_write_custody(root):
                    (root / "astrid_motion").mkdir()
                    (root / "astrid_motion/new-user.bin").write_bytes(b"created during recovery\x00\xff")
                written.append(files(root))
            try:
                transaction.rollback_group(**request, on_prepared=on_prepared)
            except PackInstallError as exc:
                assert exc.code == "rollback_conflict"
            else:
                raise AssertionError("Prepared rollback overwrote a newly created object")
            assert len(written) == 1 and files(root) == written[0]
            passed("rollback-prepared-callback-rechecks-absence", preserved_files=written[0])

        for tombstone in (False, True):
            with isolated("rollback-control-" + str(tombstone)) as root:
                record, request = removed(root, tombstone=tombstone)
                restored = transaction.rollback_group(**request)
                assert restored.transaction_id != record["transaction_id"]
                if tombstone:
                    assert transaction.read_group(root).removals == ("astrid_motion",)
                    assert not (root / "astrid_motion").exists()
                    assert observed() == {"admitted": False, "quarantine": []}
                else:
                    release_manifest.verify_release_default(root / "astrid_motion", selected_pin)
                    assert observed() == {"admitted": True, "quarantine": []}
                passed("rollback-" + ("preserves-tombstone" if tombstone else "restores-genuinely-absent-default"),
                       transaction=restored.transaction_id)
        preserved_selection()
        evidence["result"] = "PASS"
    except BaseException:
        evidence["result"] = "FAIL"
        evidence["failure"] = traceback.format_exc()
        raise
    finally:
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
