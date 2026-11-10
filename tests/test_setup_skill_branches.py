"""CPU-only checks of the setup guide's decision branches and command surfaces."""
from __future__ import annotations

import json
import csv
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from astrid.core.generation import engine_readiness
from astrid.packs.vibecomfy import local as local_scan
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)
from tests.packs.test_vibecomfy_local import _ComfyHandler, _server


_SETUP = Path(__file__).resolve().parents[1] / "docs" / "setup" / "SKILL.md"
_GENERATION = (Path(__file__).resolve().parents[1] / "astrid" / "packs" / "generation" /
               "docs" / "SKILL.md")


def _guide_command(command_fragment: str, document: Path = _SETUP) -> str:
    text = document.read_text()
    candidates = re.findall(r"```[^\n]*\n(.*?)\n```", text, re.S)
    candidates += re.findall(r"`([^`\n]+)`", text)
    return next(value.strip() for value in candidates if command_fragment in value)


def _launcher_args(command: str) -> list[str]:
    """Drop the launcher path from a Linux guide command, retaining CLI argv."""
    return shlex.split(command)[1:]


def _guide_argv(fragment: str, document: Path = _SETUP) -> list[str]:
    command = _guide_command(fragment, document)
    line = next(line for line in command.splitlines() if fragment in line)
    return _launcher_args(line)


def _assert_command_has_local_gate(markdown: str, command_fragment: str, gate_fragment: str,
                                   *, exact_line: bool = False) -> None:
    blocks = list(re.finditer(r"```[^\n]*\n(.*?)\n```", markdown, re.S))
    matching = [block for block in blocks if any(
        (line.strip() == command_fragment if exact_line else command_fragment in line)
        for line in block.group(1).splitlines()
    )]
    assert matching, f"guide command missing: {command_fragment}"
    for block in matching:
        before = markdown[:block.start()]
        headings = list(re.finditer(r"^##\s+.+$", before, re.M))
        governing_text = before[headings[-1].end():] if headings else before
        normalize = lambda value: " ".join(value.casefold().split())
        assert normalize(gate_fragment) in normalize(governing_text), (
            f"approval wording {gate_fragment!r} does not govern {command_fragment!r}"
        )


def test_generation_skill_fenced_scan_command_uses_real_cli_parser(tmp_path, monkeypatch, capsys):
    server, thread = _server(tmp_path)
    try:
        # The documented command scans default ports. Add the CLI's documented
        # testable --ports override so the fake server can use an ephemeral port.
        monkeypatch.setattr(local_scan, "_PINOKIO_INFO_URL", "http://127.0.0.1:1/pinokio/info")
        command = _guide_command("python3 -m astrid.packs.vibecomfy.local scan", _GENERATION)
        argv = shlex.split(command)[3:] + ["--ports", str(server.server_address[1])]
        assert local_scan.main(argv) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["errors"] == []
        assert result["servers"][0]["workflows"] == ["portrait_flux.json", "nested/wan_i2v.json"]
        assert result["servers"][0]["url"] == f"http://127.0.0.1:{server.server_address[1]}"
        assert set(_ComfyHandler.requested) == {
            "/system_stats", "/internal/folder_paths", "/userdata?dir=workflows",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_setup_engine_fenced_command_reaches_real_parser_through_fake_launcher(tmp_path):
    data = tmp_path / "data"
    launcher = data / "astrid" / "run-astrid"
    launcher.parent.mkdir(parents=True)
    call_log = tmp_path / "launcher-argv"
    setter_log = tmp_path / "credential-setter-called"
    launcher.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(call_log))}\n"
        f"if [ \"$1\" = credential ]; then touch {shlex.quote(str(setter_log))}; fi\n"
        f"exec {shlex.quote(sys.executable)} -c "
        "'import sys; from astrid.core.gateway import main; "
        "raise SystemExit(main(sys.argv[1:]))' \"$@\"\n"
    )
    launcher.chmod(0o755)
    root = tmp_path / "engine"
    command = _guide_command("engines install comfyui --dry-run")
    env = {**os.environ, "XDG_DATA_HOME": str(data), "ASTRID_ENGINE_ROOT": str(root),
           "PYTHONPATH": str(_SETUP.parents[2])}
    result = subprocess.run(command, shell=True, cwd=_SETUP.parents[2], env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert call_log.read_text().splitlines() == ["engines", "install", "comfyui", "--dry-run"]
    output = json.loads(result.stdout)
    assert output["recipe"]["engine"] == "comfyui"
    assert output["plan"]["steps"]
    assert output["disk_check"]["status"] in {"ok", "blocked"}
    assert not root.exists()
    assert not setter_log.exists()


@pytest.mark.parametrize("memory_mib, capable", [(11445, True), (11444, False)])
def test_nvidia_fenced_command_and_csv_cross_the_12_decimal_gb_boundary(tmp_path, memory_mib, capable):
    smi = tmp_path / "nvidia-smi"
    argv_log = tmp_path / "nvidia-argv"
    smi.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        f"echo 'Synthetic GPU, {memory_mib} MiB'\n"
    )
    smi.chmod(0o755)
    command = _guide_command("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    result = subprocess.run(command, shell=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert argv_log.read_text().splitlines() == ["--query-gpu=name,memory.total", "--format=csv,noheader"]
    name, memory = next(csv.reader([result.stdout.strip()], skipinitialspace=True))
    parsed_mib = int(memory.removesuffix(" MiB"))
    assert name == "Synthetic GPU"
    assert parsed_mib == memory_mib
    # The guide says decimal GB; nvidia-smi reports MiB. Convert the actual
    # MiB count to bytes before applying 12,000,000,000 bytes.
    observed_bytes = parsed_mib * 1024 * 1024
    assert (observed_bytes >= 12_000_000_000) is capable


def test_engine_readiness_parser_uses_its_distinct_query(tmp_path):
    argv_log = tmp_path / "readiness-argv"
    smi = tmp_path / "nvidia-smi"
    smi.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        "echo '550.54.15, 12288'\n"
    )
    smi.chmod(0o755)
    observed = engine_readiness.probe_hardware(time.monotonic() + 5, executable=str(smi))
    assert argv_log.read_text().splitlines() == [
        "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits",
    ]
    assert observed["vram_bytes"] == 12 * 1024**3


def test_runpod_machine_refuses_claim_without_paid_consent(machine_case):
    from dataclasses import replace
    from astrid.packs.runpod.target_selection import PaidSelectionError

    case = machine_case
    job = case.job()
    job = replace(job, policy={**job.policy, "consent": []})
    before = case.creates
    with pytest.raises(PaidSelectionError):
        case.machine.claim(job)
    assert case.creates == before


def test_runpod_setter_guide_command_uses_hidden_prompt_without_key_in_argv_or_env(
    monkeypatch, capsys, tmp_path,
):
    from astrid.core.util import credential_store

    command = _guide_command('credential set runpod')
    argv = _launcher_args(command)
    assert argv == ["credential", "set", "runpod"]
    key = "fake-test-runpod-secret"
    prompt_calls: list[str] = []
    stored: list[tuple[str, str]] = []

    def hidden_prompt(prompt: str) -> str:
        prompt_calls.append(prompt)
        return key

    monkeypatch.setattr(credential_store.getpass, "getpass", hidden_prompt)
    monkeypatch.setattr(credential_store, "store_credential",
                        lambda provider, value: (stored.append((provider, value)) or tmp_path / "fake.env"))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert credential_store.main(argv[1:]) == 0
    output = capsys.readouterr().out
    assert prompt_calls == ["Credential for runpod: "]
    assert stored == [("runpod", key)]
    assert key not in " ".join(argv)
    assert key not in " ".join(os.environ.values())
    assert key not in output


def test_runpod_setup_commands_reach_real_generation_parsers():
    from astrid.core.generation import cli as generation_cli

    parser = generation_cli._parser()
    checkout = _guide_argv("generate settings checkout --file /absolute/path/generation.json")
    checkin = _guide_argv("generate settings checkin /absolute/path/generation.json")
    checkout_args = parser.parse_args(checkout[1:])
    checkin_args = parser.parse_args(checkin[1:])
    assert (checkout_args.modality, checkout_args.settings_action, checkout_args.file) == (
        "settings", "checkout", "/absolute/path/generation.json")
    assert (checkin_args.modality, checkin_args.settings_action, checkin_args.file) == (
        "settings", "checkin", "/absolute/path/generation.json")

    offer = _guide_argv("generate describe --placement runpod --engine comfyui --json")
    offer_args = parser.parse_args(offer[1:])
    assert (offer_args.modality, offer_args.placement, offer_args.engine, offer_args.json) == (
        "describe", "runpod", "comfyui", True)

    paid = _guide_argv("generate image --entry ENTRY --inputs-json /absolute/path/inputs.json")
    paid = [arg.replace("FUTURE_UTC_DEADLINE", "2099-01-01T00:00:00Z")
            .replace("OFFER_DIGEST", "fake-offer")
            .replace("PREPARATION_SECONDS", "60")
            .replace("EXECUTION_SECONDS", "60")
            .replace("IDLE_SECONDS", "1800") for arg in paid]
    paid_args = parser.parse_args(paid[1:])
    assert paid_args.modality == "image"
    assert paid_args.placement == "runpod" and paid_args.consent == ["paid"]
    assert paid_args.max_usd == 2 and paid_args.offer_digest == "fake-offer"


def test_runpod_lease_guide_commands_parse_without_provider_access(monkeypatch, capsys):
    from astrid.core import machines_cli

    list_command = _guide_argv("machines --json")
    monkeypatch.setattr(machines_cli, "_leases", lambda: [{"id": "fake-pod", "kind": "runpod",
                                                              "gpu": "fake", "state": "idle",
                                                              "live": True, "cost_usd": 0}])
    assert machines_cli.main(list_command[1:]) == 0
    assert "fake-pod" in capsys.readouterr().out

    release = _guide_argv("machines release ID --json")
    calls: list[bool] = []
    rows = iter([
        [{"id": "fake-pod", "kind": "runpod", "gpu": "fake", "state": "idle", "live": True}],
        [{"id": "fake-pod", "kind": "runpod", "gpu": "fake", "state": "ended", "live": False}],
    ])
    monkeypatch.setattr(machines_cli, "_leases", lambda: next(rows))
    assert machines_cli.main([release[1], "fake-pod", *release[3:]],
                             release=lambda: calls.append(True)) == 0
    assert calls == [True]


def test_install_and_paid_run_commands_are_locally_gated_by_the_guide():
    markdown = _SETUP.read_text()
    gates = [
        ("curl -fsSL https://astrid.haus/install | sh -s -- --yes",
         "Run one install command for the user's OS, after consent"),
        ("https://astrid.haus/install.ps1))) --yes",
         "Run one install command for the user's OS, after consent"),
        ('"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui',
         "Offer installation and run the following Linux command only on its own yes", True),
        ('"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkout',
         "With a separate yes"),
        ('"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkin',
         "With a separate yes"),
        ("generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --placement runpod",
         "Obtain a fresh paid consent for that exact offer and bounds"),
    ]
    for gate in gates:
        command, wording, *exact = gate
        _assert_command_has_local_gate(markdown, command, wording, exact_line=bool(exact))


def test_sensitive_command_gate_assertion_fails_when_yes_wording_is_removed():
    markdown = _SETUP.read_text()
    command = '"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui'
    gate = "Offer installation and run the following Linux command only on its own yes"
    _assert_command_has_local_gate(markdown, command, gate, exact_line=True)
    without_gate = markdown.replace("Offer installation and run the following Linux\ncommand only on its own yes",
                                    "Offer installation and run the following Linux command", 1)
    with pytest.raises(AssertionError, match="does not govern"):
        _assert_command_has_local_gate(without_gate, command, gate, exact_line=True)
