"""Portable generation rows and package probes for source/installed selectors.

Reuse A10/pack fake tests for execution. These probes never install a package;
the existing installed lane must provision its candidate before selecting it.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from tests.lanes import has_installed_astrid

REPO = Path(__file__).resolve().parents[1]
JOURNEYS = json.loads((REPO / "tests/fixtures/generation/agent_journeys.json").read_text())
ASSERTIONS = json.loads((REPO / "tests/fixtures/a09_scenario_assertions.json").read_text())
BY_ID = {row["id"]: row for row in ASSERTIONS["cases"]}


@pytest.mark.parametrize("row", JOURNEYS["cases"], ids=lambda row: row["id"])
def test_journey_row_has_owned_assertions_and_selectors(row):
    assert JOURNEYS["schema"] == "generation-agent-journeys-v1"
    assert JOURNEYS["phase"] == 1 and JOURNEYS["engines"] == ["comfyui"]
    assert row["engine"] == "comfyui"
    assert row["execution_state"] == "not_run" and row["gpu_status"] == "not run"
    assert row["stubs"]
    for selector in row["selectors"]:
        assert selector in JOURNEYS["selectors"]
    if "assertion_ref" in row:
        assert row["assertion_ref"]["file"] == "tests/fixtures/a09_scenario_assertions.json"
        contract = BY_ID[row["assertion_ref"]["case_id"]]
        assert contract["id"] == row["id"]
        assert contract["status"] == row["status"] == "blocked"
        assert contract["required_evidence_fields"] and contract["dependencies"]
    else:
        contract = row
    checks = contract["assertions"]
    assert checks and len({check["id"] for check in checks}) == len(checks)
    assert all(check["check"].strip() for check in checks)


def test_fixture_ids_and_selector_nodes_are_complete():
    ids = [row["id"] for row in JOURNEYS["cases"]]
    assert JOURNEYS["case_count"] == len(ids) == len(set(ids)) == 28
    assert ASSERTIONS["case_count"] == len(BY_ID) == 6
    assert set(BY_ID) == {
        "human-image", "managed-missing-model", "workflow-input", "failure-owned-release",
        "A09-H3-public-generate", "A09-H3-C0-approved",
    }
    assert sum(row_id.startswith("R05-") for row_id in ids) == 8
    assert sum(row_id.startswith("R06-") for row_id in ids) == 5
    assert sum(row_id.startswith("R04-") for row_id in ids) == 8
    assert set(JOURNEYS["selectors"]) == {"cpu", "installed", "runpod-fake", "runpod-installed-fake"}
    for selector in JOURNEYS["selectors"].values():
        assert selector["marker"] == "not integration" and selector["tests"]
        for node in selector["tests"]:
            file, *function = node.split("::")
            assert (REPO / file).is_file(), node
            if function:
                tree = ast.parse((REPO / file).read_text())
                assert function == [function[0]]
                assert any(isinstance(item, ast.FunctionDef) and item.name == function[0]
                           for item in tree.body), node


def test_c0_digest_gate_retains_approved_media_and_pending_proof():
    row = BY_ID["A09-H3-C0-approved"]
    hashes = {check["id"]: check["expected_sha256"] for check in row["input_hash_assertions"]}
    assert hashes == {
        "source-sha256": "bc270f1a0e9c0c336e3ba85f0febbcee21d6152480443c21eb2ca5bd9f9c89c7",
        "mask-sha256": "95f2931e6aca567c29ddc8fa62f84a4d03ebd24400645bac5890a9937eb21e04",
        "request-sha256": "03f36b4073273bafa02549fdbedf2124dcd188b9d99c13f77e906b5416d5a03d",
        "asset-map-sha256": "60d003701a2aab017b789549d3998c7cc0d6d3611f6fd52d461079d8c2cb4524",
    }
    gate = ASSERTIONS["c0_batch_gate"]
    assert gate["approved_input_sha256"] == row["input"]["sha256"] == hashes["source-sha256"]
    assert gate["expected_media"] == row["expected_media"] == {
        "frames": 48, "width": 1024, "height": 576, "frame_rate_fps": 24,
        "audio": "present", "audio_action": "preserve",
    }
    assert gate["approved_replacement"] is None
    assert row["status"] == "blocked"
    assert row["actual_evidence"]["output_sha256"] is None
    assert all(row["actual_evidence"][key] is None for key in ("setup", "cost", "release_tuple"))
    assert "request_metadata" not in row and "asset_map_metadata" not in row
    release = BY_ID["failure-owned-release"]
    assert {check["id"] for check in release["assertions"]} >= {
        "owned-release-readback", "measured-vram-release", "human-server-preserved",
        "provider-absence-if-applicable",
    }


def test_generation_mcp_selectors_preserve_paid_and_release_controls():
    from astrid import mcp_server
    from astrid.core.generation.cli import _parser

    tools = mcp_server._generation_tools()
    assert {"generate_list", "generate_targets", "generate_describe", "generate_launch",
            "generate_image", "generate_video", "generate_workflow", "generate_release"} <= set(tools)
    argv = mcp_server.argv_for(tools["generate_image"], {
        "entry": "z-image", "mode": "t2i", "target": "runpod", "consent": ["paid"],
        "max_usd": 2.0, "deadline": "2099-01-01T00:00:00Z", "no_wait": True,
    })
    parsed = _parser().parse_args(argv[1:])
    assert (parsed.entry, parsed.target, parsed.consent, parsed.max_usd, parsed.deadline,
            parsed.no_wait) == ("z-image", "runpod", ["paid"], 2.0, "2099-01-01T00:00:00Z", True)
    release = mcp_server.argv_for(tools["generate_release"], {
        "engine": "comfyui", "target": "runpod", "no_wait": True,
    })
    parsed = _parser().parse_args(release[1:])
    assert (parsed.engine, parsed.target, parsed.no_wait) == ("comfyui", "runpod", True)


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def _package_probe(tmp_path, source, *, saved_target=False):
    profile = os.environ.get("ASTRID_HOST_EXECUTION_PROFILE", "source")
    assert profile in {"source", "installed"}
    if profile == "installed":
        assert has_installed_astrid(), "Provision the existing installed lane before selecting it"
        package_root = Path(sysconfig.get_path("purelib"))
    else:
        package_root = REPO
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    if saved_target:
        config = Path.home() / ".vibecomfy/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[comfyui]\nowner = "human"\nurl = "http://127.0.0.1:8199"\n')
    # Retain the interpreter's provisioned dependencies. The explicitly chosen
    # package root is first; the child asserts its actual imported origin.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ASTRID_", "VIBECOMFY_")) and key != "PYTHONPATH"}
    env.update(PYTHONDONTWRITEBYTECODE="1", ASTRID_HOST_EXECUTION_PROFILE=profile,
               ASTRID_HOME=str(tmp_path / "astrid-home"),
               ASTRID_STATE_HOME=str(tmp_path / "state"),
               ASTRID_WORKSPACE_CONFIG_DIR=str(tmp_path / "workspace-config"),
               ASTRID_RUNTIME_DATA_ROOT=str(tmp_path / "uncreated-runtime"),
               PYTHONPATH=str(package_root))
    before = _snapshot(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", source], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert Path(result["origin"]).resolve() == (package_root / "astrid/__init__.py").resolve()
    assert _snapshot(tmp_path) == before
    return result


_READ_ONLY_PREAMBLE = r"""
import io, json, socket
from contextlib import redirect_stdout, nullcontext
from types import SimpleNamespace
from pathlib import Path
import astrid
from astrid.core import gateway
from astrid.core.generation import cli
from astrid.core.generation import listing
from astrid.core.pack import source_setup
def forbidden(*args, **kwargs):
    raise AssertionError('probe attempted network, Runtime open or provisioning')
socket.socket.connect = forbidden
socket.create_connection = forbidden
source_setup.provision = forbidden
cli._open_client = forbidden
# Use the existing listing seams for a tiny offline catalog and empty target
# capture. The installed gateway, parser and readiness projection remain real.
listing._targets = lambda: []
listing.catalog_rows = lambda: [dict(id='a08-offline-image', kind='image', mode='t2i',
    backend='', template='', source='fixture', availability='fixture-only', reason='offline fixture')]
def command(argv):
    output = io.StringIO()
    with redirect_stdout(output):
        code = gateway.main(['generate', *argv, '--json'])
    assert code == 0, output.getvalue()
    return json.loads(output.getvalue())
"""


@pytest.mark.parametrize("saved_target", [False, True])
def test_generation_package_probe(tmp_path, saved_target):
    result = _package_probe(tmp_path, _READ_ONLY_PREAMBLE + """
from importlib import resources
listing = command(['list'])
targets = command(['targets'])
assert listing[0]['id'] == 'a08-offline-image'
root = resources.files('astrid')
for name in ('_core', 'generation', 'vibecomfy'):
    assert root.joinpath('packs', name, 'docs', 'SKILL.md').read_text(encoding='utf-8').strip()
print(json.dumps({'origin': astrid.__file__, 'skills': ['_core', 'generation', 'vibecomfy'],
                  'listing': listing, 'targets': targets}))
""", saved_target=saved_target)
    assert {"_core", "generation", "vibecomfy"} <= set(result["skills"])
    if saved_target:
        assert result["targets"]["target"]["owner"] == "human"
        assert result["targets"]["target"]["endpoint"] == "http://127.0.0.1:8199"


def test_runpod_package_fake_owner(tmp_path):
    result = _package_probe(tmp_path, _READ_ONLY_PREAMBLE + """
events = []
class Owner:
    def describe(self):
        events.append('describe')
        return {'engine': 'comfyui', 'placement': 'runpod', 'pod_id': 'fake-pod-a'}
    def release(self, *, wait):
        events.append(['release', wait])
        return {'task_id': 'release-task-a', 'state': 'cleanup_pending',
                'idempotency_key': 'runpod-release:operation-a'}
cli._open_client = lambda: nullcontext(SimpleNamespace(runpod_generation_owner=Owner()))
description = command(['describe', '--target', 'runpod'])
release = command(['release', '--engine', 'comfyui', '--target', 'runpod', '--no-wait'])
assert events == ['describe', ['release', False]]
print(json.dumps({'origin': astrid.__file__, 'description': description, 'release': release}))
""", saved_target=True)
    assert result["description"]["placement"] == "runpod"
    assert result["release"]["state"] == "cleanup_pending"
    assert result["release"]["idempotency_key"] == "runpod-release:operation-a"
