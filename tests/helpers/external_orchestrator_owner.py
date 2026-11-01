"""Prepare Runtime-owned source custody for an external orchestrator test."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import pickle
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse

RUNTIME_F320 = "f3208b26b53c4d7053e5464eaa7c5e15b1505812"
_RUNTIME_FIXTURE_DIGESTS = {
    "test_external_pack_sources": "613b466d815770c327a1b761763d412891fd649369cf3bf3f0e7a280b6ece8e7",
    "test_verified_host_launch": "10beeb007a360e04a266fe767ec8da6df9f120185855c2d528fde69c93d32870",
    "test_remote_worker_activation": "d70aef6df8cfac43045ed99b0dc51d325a8812c563b0b738a4fc08d3b5f3d459",
    "http_helpers": "0eebd43493e1d66eeb9d6aabc741de6e47cd582df62a9b7fd3f789d3784ea3b9",
    "astrid_checkout": "68a9352f2b3bb101459aee6b996135e05f758e8bd12c6da8fb4e498cdf0485e7",
}


def _installed_runtime_root() -> Path | None:
    """Return the retained Runtime source snapshot for an installed child only."""
    import astrid

    runtime_module = sys.modules.get("runtime_protocol")
    if runtime_module is None and Path(astrid.__file__).resolve().parents[1].name not in {
        "site-packages",
        "dist-packages",
    }:
        return None
    if runtime_module is None:
        runtime_module = importlib.import_module("runtime_protocol")
    runtime_origin = runtime_module.__file__
    if not isinstance(runtime_origin, str) or not runtime_origin:
        raise AssertionError("runtime_protocol has no source origin")
    installed_root = Path(runtime_origin).resolve().parents[1]
    if installed_root.name not in {"site-packages", "dist-packages"}:
        return None

    import astrid_runtime
    import runtime_protocol
    import runtime_protocol.platform

    import astrid_runtime_client
    from astrid.core.source_identity import installed_runtime_origins

    expected = installed_runtime_origins(installed_root)

    actual = {
        "astrid": Path(astrid.__file__).resolve(),
        "astrid_runtime": Path(astrid_runtime.__file__).resolve(),
        "astrid_runtime_client": Path(astrid_runtime_client.__file__).resolve(),
        "runtime_protocol": Path(runtime_protocol.__file__).resolve(),
        "runtime_protocol.platform": Path(runtime_protocol.platform.__file__).resolve(),
    }
    expected = {name: str(path) for name, path in expected.items()}
    expected.update({
        "astrid": str(installed_root / "astrid" / "__init__.py"),
        "astrid_runtime_client": str(installed_root / "astrid_runtime_client" / "__init__.py"),
    })
    if {name: str(path) for name, path in actual.items()} != expected:
        raise AssertionError(f"installed production origins changed: {actual!r} != {expected!r}")

    metadata = importlib.metadata.distribution("astrid-runtime").read_text("direct_url.json")
    if not metadata:
        raise AssertionError("installed Runtime direct_url metadata is missing")
    direct_url = json.loads(metadata)
    if direct_url.get("dir_info") != {} or direct_url.get("url", "").startswith("file://") is False:
        raise AssertionError("installed Runtime source is not a retained directory snapshot")
    source_root = Path(unquote(urlparse(direct_url["url"]).path)).resolve()
    if source_root.name != "runtime" or not source_root.is_dir():
        raise AssertionError(f"installed Runtime source snapshot is invalid: {source_root}")
    marker = source_root.parent.parent / ".astrid-runtime-source-commit"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != RUNTIME_F320:
        raise AssertionError("installed Runtime source snapshot is not the authenticated f320 checkout")
    return source_root


def _runtime_fixture(name: str) -> Any:
    source_root = _installed_runtime_root()
    if source_root is None:
        from tests.core.execution.test_runtime_source_custody_d40 import (
            _runtime_fixture as load_fixture,
        )

        return load_fixture(name)

    expected_digest = _RUNTIME_FIXTURE_DIGESTS.get(name)
    if expected_digest is None:
        raise AssertionError(f"unapproved Runtime fixture requested: {name}")
    tests_root = source_root / "tests"
    path = tests_root / f"{name}.py"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
        raise AssertionError(f"Runtime fixture bytes are not the authenticated f320 fixture: {path}")
    for dependency in ("http_helpers", "astrid_checkout"):
        dependency_path = tests_root / f"{dependency}.py"
        if (not dependency_path.is_file()
                or hashlib.sha256(dependency_path.read_bytes()).hexdigest()
                != _RUNTIME_FIXTURE_DIGESTS[dependency]):
            raise AssertionError(f"Runtime fixture dependency is not the authenticated f320 fixture: {dependency_path}")

    import tests

    runtime_tests = str(tests_root)
    if runtime_tests not in tests.__path__:
        tests.__path__.insert(0, runtime_tests)
    for dependency in _RUNTIME_FIXTURE_DIGESTS:
        dependency_module = sys.modules.get(f"tests.{dependency}")
        if dependency_module is not None:
            dependency_origin = dependency_module.__file__
            if not isinstance(dependency_origin, str) or not dependency_origin:
                raise AssertionError(f"Runtime fixture dependency has no source origin: {dependency}")
            if Path(dependency_origin).resolve() != tests_root / f"{dependency}.py":
                raise AssertionError(
                    f"Runtime fixture dependency imported from an unexpected path: {dependency_origin}"
                )
    module_name = f"tests.{name}"
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)
    module_origin = module.__file__
    if not isinstance(module_origin, str) or not module_origin:
        raise AssertionError(f"Runtime fixture has no source origin: {module_name}")
    if Path(module_origin).resolve() != path:
        raise AssertionError(f"Runtime fixture imported from an unexpected path: {module_origin}")
    return module


def _orchestrator_digest(pack_root: Path) -> str:
    """Project the same captured manifest metadata used by Runtime admission."""
    import yaml

    from astrid.core.execution import generic_host as host_module
    from astrid.core.execution.orchestrator.schema import load_orchestrator_manifest

    manifest = pack_root / "orchestrators" / "demo" / "orchestrator.yaml"
    definition = load_orchestrator_manifest(manifest)
    metadata = dict(definition.metadata)
    metadata.update(
        source="folder",
        orchestrator_root=str(manifest.parent),
        folder_id=manifest.parent.name,
        manifest_file=str(manifest),
    )
    pack = yaml.safe_load((pack_root / "pack.yaml").read_text(encoding="utf-8"))
    metadata.setdefault("source_pack", str(pack.get("id") or pack_root.name))
    metadata.setdefault("pack_root", str(pack_root))
    return host_module._capability_digest(replace(definition, metadata=metadata).to_dict())


def admit_external_orchestrator_source(
    *, tmp_path: Path, monkeypatch: Any, pack_root: Path
) -> SimpleNamespace:
    """Install one genuine owner-captured source handle before host discovery."""
    installed_root = _installed_runtime_root()
    if installed_root is None:
        from tests.helpers.runtime import use_runtime

        use_runtime(RUNTIME_F320)
    from runtime_protocol import external_pack_sources as producer
    from runtime_protocol import verified_host_launch as gate
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    from astrid.core.execution import generic_host as host_module

    owner_fixture = _runtime_fixture("test_external_pack_sources")
    authority_fixture = _runtime_fixture("test_verified_host_launch")
    from tests.core.execution.test_runtime_source_custody_d40 import _repository

    monkeypatch.setattr(host_module, "_runtime_external_sources", None)
    revision, tree = _repository(pack_root)
    definition_digest = _orchestrator_digest(pack_root)
    files = {
        path.relative_to(pack_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in pack_root.rglob("*")
        if path.is_file()
    }
    grant = {
        "repository": "https://example.test/owner/tool.git",
        "checkout": str(pack_root.parent),
        "revision": revision,
        "subpath": pack_root.name,
        "tree": tree,
        "root": str(pack_root),
        "files": files,
        "definitions": [{
            "path": "orchestrators/demo/orchestrator.yaml",
            "capability_kind": "orchestrator",
            "capability_id": "external_pack.demo",
            "definition_digest": definition_digest,
        }],
    }

    authority = authority_fixture.fixture_authority(tmp_path / "authority")
    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm, support_root=tmp_path / "support")
    try:
        preparation = authority.args["preparation"]
        authority.args["config"]["support_root"] = str(service.support_root)
        preparation["profile"].update(
            workspace_uuid=service.realm["id"],
            support_root=str(service.support_root),
            host_launch={"additional_pack_roots": [{"path": str(pack_root), "files": files}]},
        )
        authority.record["selected_profile_digest"] = gate.raw_digest(preparation["profile"])
        authority.record["import_binding"]["mode"] = gate.TRUSTED_LOCAL_MODE
        producer._write_owner_grants(service, [grant], identity=owner_fixture.OWNER)

        capture = producer.OwnerSourceCapture(
            preparation=preparation,
            config=authority.args["config"],
        )
        verifier = gate.TrustedLocalHostLaunchVerifier(
            installed_release=authority.record,
            external_sources=capture,
        )
        proof = verifier.verify(**authority.args)
        _argv, payload = verifier.claim(proof, **authority.args, binding_fd=31)
        selected = gate._RuntimeExternalSources(
            gate.canonical(json.loads(payload)["external_pack_sources"])
        )
        try:
            pickle.dumps(selected)
        except TypeError:
            pass
        else:
            raise AssertionError("Runtime source handle unexpectedly became serializable")
        host_module._consume_runtime_external_sources(sources=selected)
        custody = host_module._runtime_external_sources
        custody.bind(preparation)
    finally:
        service.close()
    if installed_root is not None:
        if _installed_runtime_root() != installed_root:
            raise AssertionError("installed production origins changed during Runtime fixture setup")
    return SimpleNamespace(
        capture=capture,
        custody=custody,
        grant=grant,
        preparation=preparation,
        selected=selected,
    )
