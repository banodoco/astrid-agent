#!/usr/bin/env python3
"""Refuse real resource/compiler failures in an unselected installed candidate.

Requires the two-version public-CLI proof. Its retained, genuinely installed
candidate is reused only as a private negative-input venue; the selected old
version, its manifest, and its live collection are never changed. Negative Git
commits are test inputs, not proposed producer identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    base = args.base.absolute()
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version, current, read_json, write_json

    root = base / "program"
    old = current(root)
    assert old is not None
    recovery = read_json(base / "public-update-recovery.json")
    assert recovery["result"] == "PASS"
    candidate = Version.load(recovery["cases"][0]["candidate"])
    assert candidate != old and candidate.root.parent == old.root.parent
    os.environ.update(HOME=str(base / "home"), USERPROFILE=str(base / "home"))
    assert not (base / "data/runtime/discovery.json").exists()
    backend = Backend(root, base / "candidate-refusals.commands.log", Path(candidate.env["ASTRID_INSTALL_UV"]))
    manifest = read_json(old.root / "manifest.json")
    original_pin = manifest["packs"]["astrid_motion"]
    source = args.source.absolute()
    assert not source.exists()

    def git(*argv):
        return subprocess.check_output(["git", "-C", str(source), *argv], text=True).strip()

    subprocess.run(["git", "clone", "--no-hardlinks", original_pin["repo"], str(source)], check=True)
    git("checkout", "--detach", original_pin["commit"])
    git("checkout", "-b", "proof/negative-inputs")
    git("config", "user.name", "PACK-DELIVERY negative proof")
    git("config", "user.email", "pack-delivery-proof@example.invalid")
    component = source / "astrid_motion/rendering/elements/effects/am-sprite/component.tsx"
    original_component = component.read_bytes()
    candidate_manifest = candidate.root / "manifest.json"
    original_manifest = candidate_manifest.read_bytes()
    prepared = candidate.root / "default-packs-prepared.json"
    original_prepared = prepared.read_bytes()
    collection = base / "home/Astrid/packs"
    pointer = (root / "current").read_bytes()

    def snapshot():
        return {p.relative_to(collection).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in collection.rglob("*") if p.is_file()}

    before = snapshot()
    evidence = {"selected": old.document(), "candidate": candidate.document(),
                "source_parent": original_pin["commit"], "cases": []}
    cases = (
        ("undeclared-resource", b"\nimport {impossible} from './r16_undeclared_negative';\n", "Undeclared or escaping dependency"),
        ("compiler-syntax", b"\nexport const r16ProofBroken = ;\n", "failed to compile"),
    )
    try:
        for name, mutation, expected in cases:
            component.write_bytes(original_component + mutation)
            git("add", "astrid_motion")
            git("commit", "-m", "Negative proof input: " + name)
            commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD:astrid_motion")
            digest = backend.run([candidate.python, "-I", "-c",
                "from astrid.core.pack.identity import pack_digest;from pathlib import Path;import sys;print(pack_digest(Path(sys.argv[1])))",
                source / "astrid_motion"], extra_env=candidate.env, separate_stderr=True).strip()
            negative = {**manifest, "packs": {"astrid_motion": {**original_pin,
                        "repo": source.as_uri(), "commit": commit, "digest": digest}}}
            write_json(candidate_manifest, negative)
            write_json(base / (name + ".manifest.json"), negative)
            log_offset = backend.log.stat().st_size if backend.log.exists() else 0
            try:
                backend.prepare_default_packs(candidate)
            except subprocess.CalledProcessError as exc:
                # Backend keeps machine stdout separate and writes real stderr
                # to its command log; retain that exact new diagnostic slice.
                output = backend.log.read_bytes()[log_offset:].decode("utf-8", "replace")
                (base / (name + ".failure.log")).write_text(output)
                assert exc.returncode != 0 and expected in output, output
                exit_code = exc.returncode
            else:
                raise AssertionError("Invalid candidate preparation succeeded: " + name)
            assert current(root) == old and (root / "current").read_bytes() == pointer
            assert snapshot() == before
            assert prepared.read_bytes() == original_prepared
            assert not (candidate.root / "default-packs-stage/packs/astrid_motion").exists()
            evidence["cases"].append({"name": name, "status": "PASS", "exit": exit_code,
                "negative_source_commit": commit, "negative_source_tree": tree,
                "negative_source_digest": digest, "observed_refusal": expected,
                "selected_version_and_collection_preserved": True})
            write_json(base / "candidate-refusals.json", evidence)
            print(json.dumps(evidence["cases"][-1]), flush=True)
    finally:
        candidate_manifest.write_bytes(original_manifest)
    assert current(root) == old and snapshot() == before
    evidence["result"] = "PASS"
    write_json(base / "candidate-refusals.json", evidence)


if __name__ == "__main__":
    main()
