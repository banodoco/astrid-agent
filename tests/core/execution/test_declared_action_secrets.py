"""PK-7: a secret reaches only an action that declares it.

One node per refusal case. The host resolves credentials only for names the
canonical action manifest declares (``actions.<id>.secrets``); the capability
matrix, ambient process variables and explicit child variables cannot widen it.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from astrid.pack_api import VERSION
from astrid.core.execution.generic_host import GenericPackHost, _required_secret_names
from astrid.core.subprocess_env import SubprocessEnvPolicyError, build_child_subprocess_env

SECRET = "fixture-secret-value-7f3a"
OTHER = "other-secret-value-91bc"


def _pack(root: Path) -> Path:
    pack = root / "secretfixture"
    pack.mkdir(parents=True)
    action = {
        "description": "Fixture", "short_description": "Fixture",
        "invocation": {"kind": "command", "command": {"argv": ["echo", "fixture"]}},
        "inputs": [], "outputs": [],
    }
    manifest = {
        "schema_version": 3, "id": "secretfixture", "name": "Secret fixture", "version": "1.0.0",
        "pack_api": ".".join(map(str, VERSION)), "documentation": {"kind": "none", "reason": "PK-7 fixture."},
        "actions": {
            "declares": {**action, "secrets": ["FIXTURE_API_KEY"]},
            "silent": dict(action),
        },
    }
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


@pytest.fixture
def host(tmp_path: Path) -> GenericPackHost:
    root = _pack(tmp_path / "packs")
    host = GenericPackHost(pack_roots=[root],
                           credential_source={"FIXTURE_API_KEY": SECRET, "OTHER_API_KEY": OTHER})
    host.discover()
    return host


def _child(host: GenericPackHost, capability: str, tmp_path: Path):
    attempt = tmp_path / "attempt" / capability
    attempt.mkdir(parents=True)
    return host._child_environment(host.capabilities[capability], attempt, allow_runtime_connection=False)


def test_declaring_action_receives_only_its_declared_secret(host, tmp_path):
    env, secrets = _child(host, "secretfixture.declares", tmp_path)
    assert secrets == {"FIXTURE_API_KEY": SECRET}
    assert env.get("FIXTURE_API_KEY") == SECRET
    assert OTHER not in env.values()


def test_undeclaring_sibling_action_receives_no_secret(host, tmp_path):
    env, secrets = _child(host, "secretfixture.silent", tmp_path)
    assert secrets == {}
    assert "FIXTURE_API_KEY" not in env and SECRET not in env.values() and OTHER not in env.values()


def test_matrix_required_env_cannot_widen_action_secrets(host):
    record = host.capabilities["secretfixture.silent"]
    widened = dataclasses.replace(record, matrix={"required_env": ["OTHER_API_KEY"]})
    assert widened.matrix["required_env"] == ["OTHER_API_KEY"]
    assert _required_secret_names(widened) == ()


def test_ambient_process_secret_is_not_inherited(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OTHER)
    env = build_child_subprocess_env(base={"PATH": "/usr/bin", "OPENAI_API_KEY": OTHER}, parent={})
    assert "OPENAI_API_KEY" not in env


def test_undeclared_secret_value_is_refused():
    with pytest.raises(SubprocessEnvPolicyError, match="secret env supplied without declaration: FIXTURE_API_KEY"):
        build_child_subprocess_env(base={}, parent={}, secret_values={"FIXTURE_API_KEY": SECRET}, declared_secrets=())


def test_secret_in_explicit_child_env_is_refused():
    with pytest.raises(SubprocessEnvPolicyError, match="must be supplied through secret_values"):
        build_child_subprocess_env(base={}, parent={}, explicit_env={"FIXTURE_API_KEY": SECRET})
