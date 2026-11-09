"""The install transaction records capability definitions for every pack, bound in the group state."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.pack.install import PackInstallError, _git_source
from astrid.core.pack.transaction import install_group, read_group

from tests.core.pack.test_transaction import GROUP, Repository, git, install, packs_root, repo  # noqa: F401


def _add_executor(repo: Repository, pack_id: str = "alpha") -> None:
    executor = repo.work / "packs" / pack_id / "executors" / "echo"
    executor.mkdir(parents=True, exist_ok=True)
    (executor / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": f"{pack_id}.echo", "name": "Echo", "kind": "external", "version": "1.0",
        "command": {"argv": [sys.executable, "-c", "pass"]}, "outputs": [],
    }), encoding="utf-8")
    repo.publish()


def _definitions(packs_root: Path, pack_id: str) -> tuple[dict, bytes]:
    raw = (packs_root / pack_id / ".build" / "definitions.json").read_bytes()
    return json.loads(raw), raw


def test_every_installed_pack_gets_definitions_bound_in_the_group_state(repo, packs_root):
    _add_executor(repo)
    install(repo, packs_root)
    snapshot = read_group(packs_root)
    for pack_id, subpath in GROUP.items():
        record, raw = _definitions(packs_root, pack_id)
        assert record["version"] == 1
        assert record["repository"] == _git_source(str(repo.bare))
        assert re.fullmatch(r"[0-9a-f]{40}", record["revision"]) and record["revision"] == repo.commit
        assert record["tree"] == git(repo.work, "rev-parse", f"{repo.commit}:{subpath}")
        assert record["subpath"] == subpath
        assert record["source"] == {"kind": "git", "repository": record["repository"],
                                    "revision": record["revision"], "tree": record["tree"]}
        # Bound by digest in the group state, browser build or not.
        assert snapshot.admissions[pack_id]["definitionsDigest"] == hashlib.sha256(raw).hexdigest()
    assert [(row["capability_kind"], row["capability_id"], row["path"])
            for row in _definitions(packs_root, "alpha")[0]["definitions"]] == [
        ("executor", "alpha.echo", "executors/echo/executor.yaml")]
    assert _definitions(packs_root, "shared")[0]["definitions"] == []


def test_definition_digests_equal_the_hosts_select_digest(repo, packs_root, monkeypatch, tmp_path):
    _add_executor(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    install(repo, packs_root)
    host = GenericPackHost(pack_roots=[packs_root])
    host.discover()
    recorded = {row["capability_id"]: row["definition_digest"] for row in _definitions(packs_root, "alpha")[0]["definitions"]}
    assert recorded["alpha.echo"] == host.capabilities["alpha.echo"].capability_digest


def test_install_round_trip_updates_and_reverifies_the_record(repo, packs_root):
    _add_executor(repo)
    install(repo, packs_root)
    first = _definitions(packs_root, "alpha")[0]["revision"]
    new_commit = repo.next_revision()
    install(repo, packs_root, revision=new_commit)
    record, raw = _definitions(packs_root, "alpha")
    assert record["revision"] == new_commit != first
    assert read_group(packs_root).admissions["alpha"]["definitionsDigest"] == hashlib.sha256(raw).hexdigest()


def test_a_tampered_definitions_record_is_refused_on_the_next_group_change(repo, packs_root):
    install(repo, packs_root)
    (packs_root / "shared" / ".build" / "definitions.json").write_text("{}", encoding="utf-8")
    repo.write_pack("alpha", "alpha-v2\n", dependencies=("shared",))
    new_commit = repo.publish()
    with pytest.raises(PackInstallError) as refused:
        install(repo, packs_root, revision=new_commit, packs={"alpha": "packs/alpha"})
    assert refused.value.code == "incomplete_artifacts"


def test_local_folder_install_records_null_provenance(repo, packs_root):
    _add_executor(repo)
    install_group(source=repo.work, revision=None, packs=GROUP, packs_root=packs_root, python_packages={})
    record, raw = _definitions(packs_root, "alpha")
    assert (record["repository"], record["revision"], record["tree"]) == (None, None, None)
    assert record["subpath"] == "packs/alpha"
    assert record["source"] == {"kind": "folder", "path": str((repo.work / "packs" / "alpha").resolve())}
    assert [row["capability_id"] for row in record["definitions"]] == ["alpha.echo"]
    assert read_group(packs_root).admissions["alpha"]["definitionsDigest"] == hashlib.sha256(raw).hexdigest()


def _fake_browser_build(request):
    """Materialize the frozen BuildResult format for a staged pack, as the real builder would."""
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def canonical(value) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    root, source = Path(request["stage_root"]), request["source_digest"]
    if request["entry"] is None and request["extension_manifest"] is None:  # not a browser pack
        return {"version": 1, "ok": True, "pack_id": request["pack_id"], "source_digest": source,
                "artifact_manifest": None}
    chunk = b"export const colour = 'blue';\n"
    chunk_disk = f".build/{source}/chunks/lazy.js"
    (root / chunk_disk).parent.mkdir(parents=True, exist_ok=True)
    (root / chunk_disk).write_bytes(chunk)
    graph = [{"logical_path": "chunks/lazy.js", "sha256": digest(chunk), "size": len(chunk), "mime": "text/javascript"}]
    graph_digest = digest(canonical(graph))
    entry = f"/* astrid-graph-index-sha256:{graph_digest} */\nimport('./chunks/lazy.js');\nexport default {{}};\n".encode()
    (root / f".build/{source}.js").write_bytes(entry)
    files = [{"path": "chunks/lazy.js", "disk": chunk_disk, "sha256": digest(chunk), "size": len(chunk),
              "mime": "text/javascript"},
             {"path": "index.js", "disk": f".build/{source}.js", "sha256": digest(entry), "size": len(entry),
              "mime": "text/javascript"}]
    artifact = {"version": 1, "packId": request["pack_id"], "sourceDigest": source, "digest": digest(entry),
                "graphIndexDigest": graph_digest, "toolchainDigest": request["toolchain"]["lock_sha256"],
                "entry": "index.js", "extensionManifest": request["extension_manifest"],
                "sharedModules": request["toolchain"]["shared_modules"], "files": files}
    return {"version": 1, "ok": True, "pack_id": request["pack_id"], "source_digest": source,
            "artifact_manifest": artifact}


def test_browser_pack_is_admitted_with_definitions_json(repo, packs_root, monkeypatch):
    """definitions.json sits beside the browser artifacts; it is neither a manifest file nor rejected as a stray."""
    from astrid.core.pack import transaction

    monkeypatch.setattr(transaction, "build_pack_assets", _fake_browser_build)
    _add_executor(repo)
    extension = {"id": "alpha.extension", "version": "1.0.0", "label": "Alpha", "apiVersion": 1, "contributions": []}
    (repo.work / "packs" / "alpha" / "entry.tsx").write_text("export default {};\n", encoding="utf-8")
    repo.publish()
    toolchain = {"lock_sha256": "b" * 64,
                 "shared_modules": {"@astrid/editor-sdk": {"url": "/sdk.js", "sha256": "c" * 64, "version": "1.0.0"}}}

    install(repo, packs_root, builds={"alpha": {"entry": "entry.tsx", "extension_manifest": extension}},
            toolchain=toolchain)

    snapshot = read_group(packs_root)
    admission = snapshot.admissions["alpha"]
    record, raw = _definitions(packs_root, "alpha")
    # Admitted as a browser pack (manifest + admission.json) and bound to its definitions record.
    assert "artifact_manifest" in admission and admission["definitionsDigest"] == hashlib.sha256(raw).hexdigest()
    build = packs_root / "alpha" / ".build"
    assert {"admission.json", "definitions.json"} <= {path.name for path in build.iterdir()}
    assert json.loads((build / "admission.json").read_text())["artifactManifestDigest"] == admission["artifactManifestDigest"]
    assert [row["capability_id"] for row in record["definitions"]] == ["alpha.echo"]
    # A later unrelated update re-verifies the retained browser admission together with definitions.json.
    new_commit = repo.next_revision()
    install(repo, packs_root, revision=new_commit, packs={"shared": "packs/shared"})
    assert read_group(packs_root).admissions["alpha"]["definitionsDigest"] == admission["definitionsDigest"]
