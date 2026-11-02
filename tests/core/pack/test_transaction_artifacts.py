"""Frozen BuildResult admission checks use real staged bytes and hash graphs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.pack.install import PackInstallError
from astrid.core.pack.transaction import _verify_artifacts


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def built(tmp_path: Path):
    """Materialize the frozen byte/manifest format, without simulating a builder."""
    source = "a" * 64
    chunk = b"export const colour = 'blue';\n"
    css = b"body { color: blue; }\n"
    files = []
    for name, data, mime in (("chunks/lazy.js", chunk, "text/javascript"),
                             ("styles/theme.css", css, "text/css")):
        disk = f".build/{source}/{name}"
        path = tmp_path / disk
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append({"path": name, "disk": disk, "sha256": digest(data), "size": len(data), "mime": mime})
    graph = [{"logical_path": f["path"], "sha256": f["sha256"], "size": f["size"], "mime": f["mime"]}
             for f in files]
    graph_digest = digest(canonical(graph))
    entry = (f"/* astrid-graph-index-sha256:{graph_digest} */\n"
             "import('./chunks/lazy.js');\nexport default {};\n").encode()
    disk = f".build/{source}.js"
    (tmp_path / disk).write_bytes(entry)
    files.append({"path": "index.js", "disk": disk, "sha256": digest(entry),
                  "size": len(entry), "mime": "text/javascript"})
    files.sort(key=lambda f: f["path"])
    extension = {"id": "fixture.extension", "version": "1.0.0", "label": "Fixture",
                 "apiVersion": 1, "contributions": []}
    shared = {"@astrid/editor-sdk": {"url": "/sdk.js", "sha256": "c" * 64, "version": "1.0.0"}}
    request = {"version": 1, "transaction_id": "d" * 32, "pack_id": "hello_ext", "stage_root": str(tmp_path),
               "source_digest": source, "entry": "entry.tsx", "extension_manifest": extension,
               "toolchain": {"lock_sha256": "b" * 64, "shared_modules": shared}}
    artifact = {"version": 1, "packId": "hello_ext", "sourceDigest": source, "digest": digest(entry),
                "graphIndexDigest": graph_digest, "toolchainDigest": "b" * 64, "entry": "index.js",
                "extensionManifest": extension, "sharedModules": shared, "files": files}
    result = {"version": 1, "ok": True, "pack_id": "hello_ext", "source_digest": source,
              "artifact_manifest": artifact}
    return request, result


def test_admission_binds_entry_lazy_assets_manifest_and_release_lock(built):
    request, result = built
    admission = _verify_artifacts(request, result)
    assert admission == {"packId": "hello_ext", "sourceDigest": request["source_digest"],
                         "digest": result["artifact_manifest"]["digest"],
                         "artifactManifestDigest": digest(canonical(result["artifact_manifest"])),
                         "artifact_manifest": result["artifact_manifest"]}


@pytest.mark.parametrize("fault", [
    "entry_tamper", "chunk_tamper", "css_tamper", "missing_chunk", "symlink", "escape",
    "case_collision", "extra_output", "wrong_source", "wrong_pack", "wrong_lock", "wrong_shared",
    "wrong_extension", "wrong_graph", "wrong_entry_graph", "bad_size", "wrong_mime", "missing_entry",
])
def test_corrupt_or_unbound_build_outputs_are_refused(built, fault):
    request, result = built
    artifact = result["artifact_manifest"]
    root = Path(request["stage_root"])
    lazy, entry, css = artifact["files"]
    if fault in {"entry_tamper", "chunk_tamper", "css_tamper"}:
        item = {"entry_tamper": entry, "chunk_tamper": lazy, "css_tamper": css}[fault]
        (root / item["disk"]).write_bytes(b"tampered")
    elif fault == "missing_chunk":
        (root / lazy["disk"]).unlink()
    elif fault == "symlink":
        path = root / lazy["disk"]
        path.unlink()
        path.symlink_to(root / entry["disk"])
    elif fault == "escape":
        lazy["disk"] = "../external.js"
    elif fault == "case_collision":
        artifact["files"].append({**lazy, "path": lazy["path"].upper()})
    elif fault == "extra_output":
        (root / ".build/unlisted.js").write_bytes(b"export default {};")
    elif fault == "wrong_source":
        result["source_digest"] = "f" * 64
    elif fault == "wrong_pack":
        artifact["packId"] = "another_pack"
    elif fault == "wrong_lock":
        artifact["toolchainDigest"] = "f" * 64
    elif fault == "wrong_shared":
        artifact["sharedModules"] = {}
    elif fault == "wrong_extension":
        artifact["extensionManifest"] = {}
    elif fault == "wrong_graph":
        artifact["graphIndexDigest"] = "f" * 64
    elif fault == "wrong_entry_graph":
        path = root / entry["disk"]
        data = path.read_bytes().replace(artifact["graphIndexDigest"].encode(), b"f" * 64)
        path.write_bytes(data)
        entry.update(sha256=digest(data), size=len(data))
        artifact["digest"] = digest(data)
    elif fault == "bad_size":
        lazy["size"] = True
    elif fault == "wrong_mime":
        lazy["mime"] = "text/html"
    elif fault == "missing_entry":
        artifact["files"].remove(entry)
    with pytest.raises(PackInstallError):
        _verify_artifacts(request, result)


def test_null_artifacts_only_succeed_for_no_browser_entry(built):
    request, result = built
    result["artifact_manifest"] = None
    with pytest.raises(PackInstallError):
        _verify_artifacts(request, result)
    request.update(entry=None, extension_manifest=None)
    # Null build results cannot smuggle browser files/records past admission.
    with pytest.raises(PackInstallError):
        _verify_artifacts(request, result)
    import shutil
    shutil.rmtree(Path(request["stage_root"]) / ".build")
    assert _verify_artifacts(request, result) is None


def stage_metadata(request, result, revision=7):
    """Frozen file contract, independently encoded rather than using the writer."""
    artifact = result["artifact_manifest"]
    manifest_bytes = canonical(artifact)
    build = Path(request["stage_root"]) / ".build"
    path = build / f'{request["source_digest"]}.artifact-manifest.json'
    path.write_bytes(manifest_bytes)
    record = {"version": 1, "packId": request["pack_id"], "sourceDigest": request["source_digest"],
              "digest": artifact["digest"], "artifactManifestDigest": digest(manifest_bytes),
              "committedRevision": revision, "transactionId": request["transaction_id"]}
    admission = build / "admission.json"
    admission.write_bytes(canonical(record))
    return path, admission


def test_metadata_is_only_accepted_for_the_expected_committed_transaction(built):
    request, result = built
    expected = _verify_artifacts(request, result)
    stage_metadata(request, result)
    with pytest.raises(PackInstallError):
        _verify_artifacts(request, result)  # Builder cannot supply its own records.
    assert _verify_artifacts(request, result, committed_revision=7) == expected


@pytest.mark.parametrize("fault", [
    "manifest_lf", "manifest_content", "missing_manifest", "manifest_symlink", "unlisted_metadata",
    "version", "packId", "sourceDigest", "digest", "artifactManifestDigest", "committedRevision",
    "transactionId", "extra_field", "duplicate_field", "entry", "chunk", "missing_chunk",
])
def test_sealed_metadata_never_weakens_artifact_or_record_checks(built, fault):
    request, result = built
    manifest_path, record_path = stage_metadata(request, result)
    record = json.loads(record_path.read_bytes())
    if fault == "manifest_lf":
        manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    elif fault == "manifest_content":
        manifest_path.write_bytes(b"{}")
    elif fault == "missing_manifest":
        manifest_path.unlink()
    elif fault == "manifest_symlink":
        manifest_path.unlink()
        manifest_path.symlink_to(record_path)
    elif fault == "unlisted_metadata":
        (record_path.parent / "extra.json").write_bytes(b"{}")
    elif fault == "duplicate_field":
        record_path.write_bytes(record_path.read_bytes()[:-1] + b',"version":1}')
    elif fault in {"entry", "chunk", "missing_chunk"}:
        item = next(f for f in result["artifact_manifest"]["files"]
                    if (f["path"] == "index.js") == (fault == "entry"))
        target = Path(request["stage_root"]) / item["disk"]
        if fault == "missing_chunk":
            target.unlink()
        else:
            target.write_bytes(b"tampered")
    else:
        record[fault] = True if fault in {"version", "committedRevision"} else "wrong"
        record_path.write_bytes(canonical(record))
    with pytest.raises(PackInstallError):
        _verify_artifacts(request, result, committed_revision=7)
