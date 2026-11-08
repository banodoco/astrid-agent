"""Set-wide provider admission checks for the host-managed TCP broker contract."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from astrid.core.execution import provider_route_resolvers
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.rendering import remotion_runtime

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "astrid" / "packs"
MATRIX = ROOT / "config" / "astrid-beta-capabilities.json"


def _provider_host(*, credentials: dict[str, str]) -> GenericPackHost:
    host = GenericPackHost(
        pack_roots=[PACKS],
        capability_matrix=MATRIX,
        credential_source=credentials,
    )
    host.discover()
    host.preflight()
    return host


def _fixture_credentials(host: GenericPackHost) -> dict[str, str]:
    names = {
        str(name)
        for record in host.capabilities.values()
        if record.adapter.family == "provider"
        for name in (
            *(record.matrix.get("required_env") or ()),
            *(record.definition.isolation.secrets_required or ()),
            *(record.definition.metadata.get("env") or ()),
            *(record.definition.metadata.get("required_env") or ()),
            *(record.definition.metadata.get("secrets_required") or ()),
        )
    }
    return {name: "provider-manifest-fixture-secret" for name in names}


# The provider records this head declares (pack set after 5d841e305 moved nine packs to
# poms_packs, and edaa8e7c1 added the hivemind pack). Explicit ids so drift names itself.
EXPECTED_REQUIRED_PROVIDER_IDS = frozenset({
    "generation.generate_audio", "generation.generate_image", "generation.generate_image_cloud_i2i",
    "generation.generate_image_edit", "generation.generate_image_openai", "generation.generate_image_upscale",
    "generation.generate_video", "training.search_loras", "training.training_run",
})

EXPECTED_PROVIDER_IDS = frozenset({
    "generation.generate_audio",
    "generation.generate_image",
    "generation.generate_image_cloud_i2i",
    "generation.generate_image_codex",
    "generation.generate_image_edit",
    "generation.generate_image_openai",
    "generation.generate_image_upscale",
    "generation.generate_video",
    "hivemind.contribute",
    "hivemind.get_item",
    "hivemind.ingest_article",
    "hivemind.ingest_workflow",
    "hivemind.ingest_youtube",
    "hivemind.refresh_media",
    "hivemind.search",
    "runpod.exec",
    "runpod.provision",
    "runpod.pull",
    "runpod.session",
    "runpod.teardown",
    "training.search_loras",
    "training.training_run",
})


def test_every_tcp_provider_manifest_declares_an_enforceable_host_broker():
    host = _provider_host(credentials={})
    providers = [record for record in host.capabilities.values() if record.adapter.family == "provider"]

    assert {record.id for record in providers} == EXPECTED_PROVIDER_IDS
    for record in providers:
        policy = record.definition.metadata.get("network_policy")
        assert isinstance(policy, dict), (record.id, "network_policy is not declared")
        protocols = {str(value).lower() for value in policy["allowed_protocols"]}
        broker = policy.get("broker")
        if protocols & {"udp", "quic"}:
            assert not record.ready
            continue
        assert isinstance(broker, dict)
        assert broker["host_managed"] is True
        assert broker["kind"] == "broker"
        assert broker["enforced"] is True
        assert broker["observable"] is True
        assert broker["route"]
        assert broker["wrapper"]
        descendant = policy.get("descendant_enforcement")
        assert isinstance(descendant, dict)
        assert descendant["kind"] == "broker"
        assert descendant["validated"] is True
        assert descendant["observable"] is True
        assert descendant["route"]
        assert descendant["wrapper"]


@pytest.mark.darwin_native
def test_all_supported_provider_routes_are_ready_and_grantable_with_declared_inputs(monkeypatch, tmp_path):
    probe = _provider_host(credentials={})
    providers = [record for record in probe.capabilities.values() if record.adapter.family == "provider"]
    declared_tools = {
        str(binary)
        for record in providers
        for binary in (
            *record.definition.isolation.binaries,
            *(record.matrix.get("required_binaries") or ()),
            *record.adapter.required_binaries,
        )
    }
    # Supply every tool named by the provider declarations so readiness cannot
    # vary with binaries installed on the machine running this contract test.
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: f"/fixture/bin/{name}"
        if name in declared_tools or name == "sandbox-exec"
        else None,
    )
    # The capture route also requires the independently provisioned Remotion
    # runtime. Give preflight a deterministic available runtime fixture so its
    # readiness does not depend on this checkout's Node installation.
    remotion_project = tmp_path / "remotion"
    monkeypatch.setattr(
        remotion_runtime,
        "remotion_runtime_status",
        lambda *, require_explicit_project=False: remotion_runtime.RemotionRuntimeStatus(
            True, remotion_project, node_executable=tmp_path / "node", node_version="fixture",
            remotion_cli=remotion_project / "cli.js",
        ),
    )
    # Dynamic RunPod routes are resolved through the live provider API. Exercise
    # grant creation with the declared handle input while keeping this contract
    # test independent of an actual pod or API credential.
    def fixture_runpod_route(handle):
        handle_path = Path(str(handle)).resolve()
        assert handle_path.is_relative_to(tmp_path.resolve())
        assert handle_path.is_file()
        return "tcp://192.0.2.1:22"

    monkeypatch.setattr(provider_route_resolvers, "resolve_runpod_pod_handle_ssh_route", fixture_runpod_route)
    host = _provider_host(credentials=_fixture_credentials(probe))
    providers = [record for record in host.capabilities.values() if record.adapter.family == "provider"]
    tcp_providers = []
    for record in providers:
        protocols = {str(value).lower() for value in record.definition.metadata["network_policy"]["allowed_protocols"]}
        if not protocols & {"udp", "quic"}:
            tcp_providers.append(record)
            assert record.ready, (record.id, record.preflight)

    assert {record.id for record in tcp_providers} == EXPECTED_PROVIDER_IDS
    for record in tcp_providers:
        policy = record.definition.metadata["network_policy"]
        declared_inputs = {}
        for name in policy.get("dynamic_url_inputs", ()):
            declared_inputs[str(name)] = "https://provider-route-fixture.invalid"
        for name in policy.get("dynamic_tcp_inputs", ()):
            handle = tmp_path / f"{record.id}-{name}.json"
            handle.write_text('{"fixture": true}\n', encoding="utf-8")
            declared_inputs[str(name)] = str(handle)
        task = {
            "task": {
                "id": f"manifest-grant-{record.id}",
                "capability": record.id,
                "spec": {"inputs": declared_inputs},
            }
        }
        token = host.request_provider_route_grant(task)
        assert token.startswith("provider-route-grant-v1.")


def test_required_provider_routes_do_not_gate_readiness_on_credentials(monkeypatch, tmp_path):
    # Hermetic: readiness must not depend on what this machine happens to have installed
    # (e.g. a `codex` binary on PATH) or exported. Hide every binary except the macOS
    # sandbox launcher, and clear every provider credential variable from the environment.
    real_which = shutil.which
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: real_which(name, *a, **k) if name == "sandbox-exec" else None
    )
    probe = _provider_host(credentials={})
    for name in _fixture_credentials(probe):
        monkeypatch.delenv(name, raising=False)
    host = _provider_host(credentials={})
    required = [
        record
        for record in host.capabilities.values()
        if record.adapter.family == "provider" and record.matrix.get("disposition") == "required"
    ]
    # The explicit required set: the providers that moved to poms (5d841e305) are not here.
    assert {record.id for record in required} == EXPECTED_REQUIRED_PROVIDER_IDS
    for record in required:
        if record.matrix.get("required_env"):
            # Missing keys are diagnostic, regardless of other readiness checks.
            assert record.preflight["credentials"]["ok"] is True, record.id
            assert record.preflight["credentials"]["missing"], record.id
        if record.matrix.get("required_binaries"):
            # Binary-gated (binaries are hidden above): unavailable because the binary is missing.
            assert record.ready is False, record.id
            assert not record.preflight["binaries"]["ok"], record.id
        else:
            # Readiness is exactly the remaining checks.
            checks = record.preflight
            ok = all(v is True or (isinstance(v, dict) and v.get("ok") is True) for v in checks.values())
            assert record.ready is ok, record.id
