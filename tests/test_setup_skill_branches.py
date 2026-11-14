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
from tests.packs.runpod.conftest import (  # noqa: F401
    lease_https_transport as lease_https_transport,
    pod_watchdog_transport as pod_watchdog_transport,
)
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)
from tests.packs.test_vibecomfy_local import _ComfyHandler, _server


_SETUP = Path(__file__).resolve().parents[1] / "docs" / "setup" / "SKILL.md"
_GENERATION = (Path(__file__).resolve().parents[1] / "astrid" / "packs" / "generation" /
               "docs" / "SKILL.md")
_RUNPOD = Path(__file__).resolve().parents[1] / "astrid" / "packs" / "runpod" / "docs" / "SKILL.md"
# The RunPod claim walkthrough moved out of the setup skill. These are the argv forms
# that walkthrough used, kept here so the parse and consent checks still run.
_CHECKOUT = ["generate", "settings", "checkout", "--file", "/absolute/path/generation.json"]
_CHECKIN = ["generate", "settings", "checkin", "/absolute/path/generation.json"]
_DESCRIBE = ["generate", "describe", "--placement", "runpod", "--engine", "comfyui", "--json"]
_CLAIM = ["machines", "claim", "--consent", "paid", "--max-usd", "2", "--deadline",
          "FUTURE_UTC_DEADLINE", "--for", "30m", "--json"]
_PAID = ["generate", "image", "--entry", "ENTRY", "--inputs-json", "/absolute/path/inputs.json",
         "--placement", "runpod", "--json"]


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
            "/system_stats", "/internal/folder_paths",
            "/userdata?dir=workflows&full_info=true&recurse=true",  # P4.11a: the scan asks for edit times (full_info)
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


def test_runpod_machine_entry_refuses_a_job_without_a_claimed_machine(machine_case):
    # Claim-first (f123e45c1): the entry refuses before any consent check; paid
    # consent itself is covered by test_target_selection (consent=()) and the CLI case below.
    from dataclasses import replace
    from astrid.packs.runpod.target_selection import PaidSelectionError

    case = machine_case
    job = case.job()
    job = replace(job, policy={**job.policy, "consent": []})
    before = case.creates
    with pytest.raises(PaidSelectionError):
        case.machine.claim(job)
    assert case.creates == before


def test_runpod_machine_refuses_claim_without_paid_consent(monkeypatch, capsys):
    from tests.packs.runpod.test_machine import _claim_cli_case

    cli, _, rows, calls, describe, claim = _claim_cli_case(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    argv = list(_CLAIM)
    consent = argv.index("--consent")
    del argv[consent:consent + 2]
    argv = [arg.replace("FUTURE_UTC_DEADLINE", "2099-01-01T00:00:00Z") for arg in argv]
    # cause 5158964ba; ruling: machines 07:40 Sat
    assert cli.main(argv[1:], describe=describe, claim=claim) == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["ok"] is False and "--consent paid" in refusal["error"]
    assert calls == [] and rows == []


def test_runpod_setter_guide_command_uses_hidden_prompt_without_key_in_argv_or_env(
    monkeypatch, capsys, tmp_path,
):
    from astrid.core.util import credential_store

    argv = _guide_argv('credential set runpod', _RUNPOD)
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


def test_runpod_setup_commands_reach_real_generation_parsers(monkeypatch, capsys):
    from astrid.core.generation import cli as generation_cli

    parser = generation_cli._parser()
    checkout_args = parser.parse_args(_CHECKOUT[1:])
    checkin_args = parser.parse_args(_CHECKIN[1:])
    assert (checkout_args.modality, checkout_args.settings_action, checkout_args.file) == (
        "settings", "checkout", "/absolute/path/generation.json")
    assert (checkin_args.modality, checkin_args.settings_action, checkin_args.file) == (
        "settings", "checkin", "/absolute/path/generation.json")

    offer_args = parser.parse_args(_DESCRIBE[1:])
    assert (offer_args.modality, offer_args.placement, offer_args.engine, offer_args.json) == (
        "describe", "runpod", "comfyui", True)

    from tests.packs.runpod.test_machine import _claim_cli_case

    cli, _, _, calls, describe, claim = _claim_cli_case(monkeypatch)
    claim_argv = [arg.replace("FUTURE_UTC_DEADLINE", "2099-01-01T00:00:00Z") for arg in _CLAIM]
    assert cli.main(claim_argv[1:], describe=describe, claim=claim) == 0
    grant = calls[-1][1]["consent"]
    paid_args = parser.parse_args(_PAID[1:])
    assert paid_args.modality == "image"
    # cause f123e45c1; ruling: machines 07:40 Sat
    assert paid_args.placement == "runpod" and grant.consent == ("paid",)
    assert grant.max_usd == 2 and grant.deadline == "2099-01-01T00:00:00Z"
    assert grant.idle_seconds == 1800 and grant.capacity_timeout_seconds == 1800
    assert json.loads(capsys.readouterr().out)["policy"]["deadline"] == "2099-01-01T00:00:00Z"
    assert not {"--consent", "--max-usd", "--deadline", "--allow-paid", "--until-release"}.intersection(_PAID)


def test_runpod_lease_guide_commands_parse_without_provider_access(monkeypatch, capsys):
    from astrid.core import machines_cli

    list_command = _guide_argv("machines --json", _RUNPOD)
    monkeypatch.setattr(machines_cli, "_leases", lambda: [{"id": "fake-pod", "kind": "runpod",
                                                              "gpu": "fake", "state": "idle",
                                                              "live": True, "cost_usd": 0}])
    assert machines_cli.main(list_command[1:]) == 0
    assert "fake-pod" in capsys.readouterr().out

    release = ["machines", "release", "ID", "--json"]
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
         "Run the following Linux command only on its own yes", True),
        ('"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings set placement',
         "On that yes, save the connection"),
        ('--apply PLAN_SHA256', "On the person's one yes"),
    ]
    for gate in gates:
        command, wording, *exact = gate
        _assert_command_has_local_gate(markdown, command, wording, exact_line=bool(exact))


def test_sensitive_command_gate_assertion_fails_when_yes_wording_is_removed():
    markdown = _SETUP.read_text()
    command = '"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui'
    gate = "Run the following Linux command only on its own yes"
    _assert_command_has_local_gate(markdown, command, gate, exact_line=True)
    without_gate = markdown.replace(gate, "Run the following Linux command", 1)
    with pytest.raises(AssertionError, match="does not govern"):
        _assert_command_has_local_gate(without_gate, command, gate, exact_line=True)


def test_runpod_setup_guide_explains_the_persisted_policy():
    # The policy walkthrough moved to the RunPod skill; setup links it.
    assert "[RunPod machine skill]" in _SETUP.read_text()
    runpod = _RUNPOD.read_text()
    assert "`machines.runpod`" in runpod
    assert "gpu_types" in runpod


def test_runpod_setup_from_pristine_settings_preserves_choices_and_quotes_without_claim(
    small_lease, generation_settings_store, tmp_path, monkeypatch, capsys,
):
    from contextlib import nullcontext
    from dataclasses import replace
    from types import SimpleNamespace
    from astrid.core.generation import cli, settings
    from astrid.packs.runpod.target_selection import PaidSelectionError, RunPodPreferences
    from astrid.sdk.client import AstridClient
    from astrid.sdk.generation import GenerationFacade

    c = small_lease
    raw = {**c.raw, "budget_cap_usd": 5.0}
    prefs = RunPodPreferences.from_mapping(raw)
    c.owner.preferences = prefs
    owner = c.fresh()
    assert settings.read(legacy_path=tmp_path / "missing").content == settings.empty_document()
    calls = []

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def invoke_result(self, action, **kwargs):
            policy = json.loads(kwargs["inputs"]["placement_policy"])
            assert RunPodPreferences.from_mapping(policy["preferences"]) == prefs
            calls.append(action)
            if action == "runpod.setup":
                result = owner.setup()
                return SimpleNamespace(ok=True, raw_result={"payload": {"target_operation": result}})
            assert action == "runpod.status" and kwargs["inputs"]["include_quote"]
            offer = c.owner.observe_offer()
            result = {"offer": {"gpu_type": offer.gpu_type, "hourly_usd": offer.hourly_usd},
                      "offer_digest": offer.digest(prefs)}
            return SimpleNamespace(ok=True, raw_result={"result": {"payload": {"target_operation": result}}})

    client = Client()
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda: client)
    runpod_doc = _SETUP.parents[2] / "astrid/packs/runpod/docs/SKILL.md"
    snippet = _guide_command('"runpod.setup", kind="action"', runpod_doc)
    exec(snippet, {"preferences": raw})
    saved = settings.read().content["machines"]["runpod"]
    assert saved == prefs.as_mapping()

    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    monkeypatch.setattr(settings, "transport_of", lambda _: generation_settings_store)
    path = tmp_path / "generation.json"
    checkout = _CHECKOUT
    checkin = _CHECKIN
    assert cli.main([arg.replace("/absolute/path/generation.json", str(path)) for arg in checkout[1:]]) == 0
    document = json.loads(path.read_text())
    document["placement"] = {"kind": "runpod"}
    path.write_text(json.dumps(document))
    assert cli.main([arg.replace("/absolute/path/generation.json", str(path)) for arg in checkin[1:]]) == 0
    assert settings.read().content["machines"]["runpod"] == saved
    offer_args = cli._parser().parse_args(_DESCRIBE[1:])
    quote = GenerationFacade().describe(placement=offer_args.placement, engine=offer_args.engine, client=client)
    assert quote["offer"]["gpu_type"] == prefs.gpu_types[0]
    grant = replace(c.grant, offer_digest=quote["offer_digest"], max_usd=5.0)
    assert c.owner.consented_offer(grant, c.owner.clock())[0] == c.offer
    with pytest.raises(PaidSelectionError, match="ceiling"):
        c.owner.consented_offer(replace(grant, max_usd=5.01), c.owner.clock())
    with pytest.raises(PaidSelectionError, match="consent"):
        c.owner.consented_offer(replace(grant, consent=()), c.owner.clock())
    assert calls == ["runpod.setup", "runpod.status"]
    assert c.creates == 0 and not c.owner.path.exists()
    capsys.readouterr()


def _generation_setup_section() -> str:
    text = _GENERATION.read_text()
    start = text.index("## Set up my generation")
    return text[start:text.index("\n## ", start + 1)]


def _offered_generate_argv(section: str, tokens: dict[str, str]) -> list[list[str]]:
    """Every inline launcher `generate ...` command the section offers, as argv after `generate`."""
    offered = []
    # Launcher form (the setup skill's Launcher paths); the Linux path is the one written out.
    for command in re.findall(r'`("\$\{XDG_DATA_HOME[^`]+run-astrid" generate [^`]+)`', section):
        offered.append([tokens.get(arg, arg) for arg in shlex.split(command)[2:]])
    return offered


def _tokens(workflow: Path, name: str = "lighthouse") -> dict[str, str]:
    return {"KIND": "image", "MODE": "t2i", "ENTRY": "z-image", "NAME": name, "FILE": str(workflow)}


def test_generation_defaults_step_offers_four_answers_each_with_one_undo():
    section = _generation_setup_section()
    step = section[section.index("4. **"):section.index("5. **")]
    assert "One question per default slot" in step
    for label in ("**Keep Astrid's**", "**Use one of yours**", "**Another shipped entry**",
                  "**Create a new default when none of those fits**", "**Save and pin**"):
        assert label in step, label
    assert step.count("Undo:") == 4


def test_generation_defaults_commands_parse_through_real_cli_and_never_queue_a_run(tmp_path):
    from astrid.core.generation import cli as generation_cli

    offered = _offered_generate_argv(_generation_setup_section(), _tokens(tmp_path / "workflow.json"))
    relevant = [argv for argv in offered if argv[0] in {"defaults", "workflow", "list"}]
    parser = generation_cli._parser()
    parsed = [parser.parse_args(argv) for argv in relevant]
    workflows = [argv for argv in relevant if argv[0] == "workflow"]
    assert any(argv[0] == "defaults" and "set" in argv for argv in relevant)
    assert any(argv[0] == "defaults" and "unset" in argv for argv in relevant)
    assert workflows and len(parsed) == len(relevant)
    for argv in workflows:
        assert "--as-entry" in argv
        assert "--dry-run" in argv or "--no-run" in argv, argv
    assert any("--dry-run" in argv for argv in workflows)
    assert any("--no-run" in argv for argv in workflows)


def test_generation_defaults_preview_and_save_run_against_fakes(tmp_path, monkeypatch, capsys):
    from astrid.core.generation import cli as generation_cli
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    # a small workflow keeps the save leg fast; the per-node cache rehash is filed against VibeComfy
    workflow = tmp_path / "workflow.json"
    workflow.write_text((Path(__file__).resolve().parent / "fixtures" / "vibecomfy_ir" / "flat.json").read_text())
    entries_dir = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(entries_dir))
    monkeypatch.setattr(generation_cli, "_open_client", lambda: pytest.fail("the setup step opened Runtime"))
    monkeypatch.setattr(generation_cli, "_generate_facade", lambda: pytest.fail("the setup step ran a workflow"))
    for argv in _offered_generate_argv(_generation_setup_section(), _tokens(workflow)):
        if argv[0] != "workflow":
            continue
        assert generation_cli.main(argv) == 0
        result = json.loads(capsys.readouterr().out)
        if "--dry-run" in argv:
            assert result["written"] is False and result["would_save_entry"] == "lighthouse"
            assert not entries_dir.exists()
        else:
            assert result == {"saved": True, "ran": False, "entry": "lighthouse", "replaced": False}
            assert (entries_dir / "lighthouse").is_dir()


def test_generation_defaults_fetch_saved_workflow_read_only_then_pins_it(
    tmp_path, monkeypatch, capsys, generation_settings_store,
):
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from astrid.core.generation import cli as generation_cli, settings
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    # a small workflow keeps the save leg fast; the per-node cache rehash is filed against VibeComfy
    body = (Path(__file__).resolve().parent / "fixtures" / "vibecomfy_ir" / "flat.json").read_bytes()
    seen: list[tuple[str, str]] = []

    class Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path))
            if self.path == "/userdata/workflows%2Fportrait_flux.json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            seen.append((self.command, self.path))
            self.send_error(405)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        command = _guide_command("userdata/workflows%2FWORKFLOW", _GENERATION)
        command = command.replace("127.0.0.1:PORT", f"127.0.0.1:{port}").replace("WORKFLOW", "portrait_flux.json")
        result = subprocess.run(command, shell=True, env={**os.environ, "WORK": str(tmp_path)},
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert seen == [("GET", "/userdata/workflows%2Fportrait_flux.json")]
        fetched = tmp_path / "workflow.json"
        assert fetched.read_bytes() == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    entries_dir = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(entries_dir))
    tokens = _tokens(fetched, name="portrait_saved")
    real_facade = generation_cli._generate_facade
    monkeypatch.setattr(generation_cli, "_generate_facade", lambda: pytest.fail("the setup step ran a workflow"))
    monkeypatch.setattr(generation_cli, "_open_client", lambda: pytest.fail("the setup step opened Runtime"))
    for argv in _offered_generate_argv(_generation_setup_section(), tokens):
        if argv[0] == "workflow":
            assert generation_cli.main(argv) == 0
            capsys.readouterr()
    assert (entries_dir / "portrait_saved").is_dir()

    class Client:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(generation_cli, "_open_client", lambda: Client())
    monkeypatch.setattr(generation_cli, "_generate_facade", real_facade)
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)
    pins = [argv for argv in _offered_generate_argv(_generation_setup_section(), tokens)
            if argv[:2] == ["defaults", "set"] and argv[-1] == "portrait_saved"]
    assert pins == [["defaults", "set", "image", "t2i", "portrait_saved"]]
    assert generation_cli.main(pins[0]) == 0
    capsys.readouterr()
    assert generation_cli.main(["defaults", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["slots"]
    assert next(row for row in rows if row["slot"] == "image.t2i")["entry"] == "portrait_saved"


_HANDOFF = "Ask your agent: set up my generation with the astrid-setup skill."
_LAUNCHER_FORMS = (
    '"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"',
    '"$HOME/.local/share/astrid/run-astrid"',
    '& "$env:LOCALAPPDATA\\Astrid\\run-astrid.cmd"',
)


def _fenced_setup_commands(markdown: str) -> list[list[str]]:
    """Every launcher-form command in a fenced block of the setup skill, as argv after the launcher."""
    commands = []
    for block in re.findall(r"```[^\n]*\n(.*?)\n```", markdown, re.S):
        for line in block.splitlines():
            for form in _LAUNCHER_FORMS:
                if line.startswith(form):
                    commands.append(shlex.split(line[len(form):].replace("& ", "", 1))
                                    if form.startswith("&") else shlex.split(line[len(form):]))
    return commands


def test_setup_skill_has_one_hand_off_sentence_and_no_runpod_walkthrough():
    source = (_SETUP.parents[2] / "astrid/packs/setup/docs/SKILL.md").read_text()
    assert source.count(_HANDOFF) == 1
    for text in (source, _SETUP.read_text()):
        assert "machines claim" not in text
        assert "credential set runpod" not in text
        assert "generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --placement runpod" not in text


def test_setup_skill_generate_commands_all_parse_through_the_real_cli():
    from astrid.core.generation import cli as generation_cli

    parser = generation_cli._parser()
    argvs = [argv for argv in _fenced_setup_commands(_SETUP.read_text()) if argv[:1] == ["generate"]]
    assert argvs, "the setup skill has no generate commands"
    for argv in argvs:
        parser.parse_args(argv[1:])
    # The only write to a ComfyUI is fetch-models with --apply, and it sits after its one yes.
    writers = [argv for argv in argvs if "--apply" in argv]
    assert [argv[1] for argv in writers] == ["fetch-models"], writers
    assert not [argv for argv in argvs if argv[1:2] == ["launch"]]


def test_setup_skill_placement_commands_run_against_a_fake_store(
    monkeypatch, capsys, generation_settings_store,
):
    from astrid.core.generation import cli as generation_cli, settings

    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)
    fixture_argv = [argv for argv in _fenced_setup_commands(_SETUP.read_text())
                    if argv[:1] == ["generate"] and argv[1:4] == ["settings", "set", "placement"]]
    assert len(fixture_argv) == 1, fixture_argv
    set_argv = [arg.replace("PORT", "8188") for arg in fixture_argv[0][1:]]
    unset_argv = ["settings", "unset", "placement"]

    class Client:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(generation_cli, "_open_client", lambda: Client())
    monkeypatch.setattr(generation_cli, "_generate_facade", lambda: pytest.fail("setup ran a generation"))
    assert generation_cli.main(set_argv) == 0
    capsys.readouterr()
    assert settings.read().content["placement"]["kind"] == "attached"
    assert generation_cli.main(unset_argv) == 0
    capsys.readouterr()
    assert not settings.read().content["placement"]  # empty: the built-in default applies again


def test_setup_skill_fetch_models_plan_and_apply_are_in_separate_blocks():
    markdown = _SETUP.read_text()
    for block in re.findall(r"```[^\n]*\n(.*?)\n```", markdown, re.S):
        lines = [line for line in block.splitlines() if "generate fetch-models" in line]
        assert not (any("--apply" in line for line in lines)
                    and any("--apply" not in line for line in lines)), block
