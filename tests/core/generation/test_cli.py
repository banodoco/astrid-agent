"""The public generate command stays a thin adapter over the SDK facade."""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli


def test_failed_runpod_command_prints_bounded_kept_machine_summary(monkeypatch, capsys):
    from astrid.packs.runpod import machine

    row = {'id': 'pod-1', 'lease': 'operation-1', 'state': 'idle', 'live': True,
           'release_at': 1800000600.0, 'cost_usd': 0.25,
           'last_job': {'generation': 2, 'outcome': 'failed',
                        'failure': {'exception': 'MachineError', 'stage': 'fetch',
                                    'code': 'generation_failed', 'category': 'execution'}}}
    monkeypatch.setattr(machine, 'journal_leases', lambda: [row])
    cli._kept_failure({('operation-1', 1)})
    message = capsys.readouterr().err
    assert 'Failure: exception=MachineError, stage=fetch, code=generation_failed' in message
    assert 'machine pod-1 kept until ' in message and '(cost so far $0.25)' in message
    assert 'reuse it or run `astrid machines release pod-1`' in message


def test_machines_table_shows_last_job_outcome():
    from astrid.core.machines_cli import render

    row = {'id': 'pod-1', 'kind': 'runpod', 'state': 'idle', 'live': True,
           'last_job': {'outcome': 'failed'}}
    table = render([row])
    assert 'LAST JOB' in table and 'failed' in table


def _facade(monkeypatch):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["dog.png"]})

    facade = SimpleNamespace(image=invoke, video=invoke)
    monkeypatch.setattr(cli, "_generate_facade", lambda: facade)
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    return calls


def test_bare_prompt_uses_auto_target_and_sdk_default_model(monkeypatch, capsys):
    # align is forwarded only when --align is given: the facade reads "not asked" as check
    # on a local target and lets RunPod align, so the CLI does not invent a value.
    calls = _facade(monkeypatch)

    assert cli.main(["image", "a dog eating an ice cream", "--project", "demo"]) == 0

    assert calls == [{
        "model": None, "placement": "auto", "dry_run": False,
        "prompt": "a dog eating an ice cream", "project": "demo",
        "client": "runtime-client",
    }]
    assert "dog.png" in capsys.readouterr().out


@pytest.mark.parametrize(("modality", "dimension_input"), [("image", "size"), ("video", "resolution")])
def test_entry_edit_and_settings_reach_the_existing_facade_once(monkeypatch, capsys, modality, dimension_input):
    calls = _facade(monkeypatch)
    from astrid.core.generation import entries

    def resolve(entry_id, values, *, mode, kind):
        assert entry_id == "flux-schnell" and mode == "edit" and kind == modality
        values = dict(values)
        width, height = values.pop("width"), values.pop("height")
        values[dimension_input] = f"{width}x{height}"
        return {"source": "catalog", "mode": mode}, values

    monkeypatch.setattr(entries, "resolve_entry_inputs", resolve)
    target = "http://127.0.0.1:8188"
    assert cli.main([
        modality, "a dog", "--entry", "flux-schnell", "--mode", "edit",
        "--placement", target, "--project", "demo", "--out", "export",
        "--edit", "reference.png", "--width", "768", "--height", "512",
        "--steps", "8", "--seed", "41", "--cfg", "1.5", "--json",
    ]) == 0
    assert calls == [{
        "entry": "flux-schnell", "mode": "edit", "placement": target,
        "dry_run": False, "prompt": "a dog", "project": "demo", "out": "export",
        "image_ref": "reference.png", dimension_input: "768x512",
        "steps": 8, "seed": 41, "guidance_scale": 1.5,
        "client": "runtime-client",
    }]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


def test_dry_run_is_forwarded_to_sdk_without_a_second_invocation(monkeypatch, capsys):
    calls = _facade(monkeypatch)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry-run connected to Runtime"))
    assert cli.main(["image", "a dog", "--model", "flux-schnell", "--placement", "managed", "--dry-run"]) == 0
    assert len(calls) == 1
    assert calls[0]["model"] == "flux-schnell"
    assert calls[0]["placement"] == "managed"
    assert calls[0]["dry_run"] is True
    assert "client" not in calls[0]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


@pytest.mark.parametrize("args", [
    ["image", "a dog", "--width", "512"],
    ["image", "a dog", "--width", "0", "--height", "512"],
    ["video", "   "],
])
def test_invalid_request_does_not_invoke(monkeypatch, capsys, args):
    calls = _facade(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert not calls
    assert capsys.readouterr().err


def test_gateway_preserves_coded_actionable_target_error(monkeypatch, capsys):
    from astrid.core import gateway
    from astrid.sdk.exceptions import CapabilityPreconditionError
    def fail(**_kwargs):
        error = CapabilityPreconditionError("No Comfy target is ready.")
        error.code = "COMFY_TARGET_UNAVAILABLE"
        error.field = "target"
        error.fix = "Use --placement http://127.0.0.1:8188 after starting ComfyUI."
        raise error

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=fail))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    assert gateway.main(["generate", "image", "a dog"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "code: target_unreachable" in captured.err
    assert "fix: astrid generate targets --placement auto" in captured.err
    assert "this is a bug" not in captured.err


def test_gateway_reports_missing_provider_key_as_terminal_precondition(monkeypatch, capsys):
    from astrid.core import gateway
    from astrid.sdk.exceptions import CapabilityPreconditionError
    events = []

    def fail(**_kwargs):
        events.append("generation-dispatched")
        error = CapabilityPreconditionError("FAL_KEY is required for this provider.")
        error.code = "GENERATION_KEY_MISSING"
        error.field = "credentials"
        error.fix = "Configure FAL_KEY and retry."
        raise error

    @contextmanager
    def open_client():
        events.append("client-open")
        try:
            yield "runtime-client"
        finally:
            events.append("client-close")

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=fail))
    monkeypatch.setattr(cli, "_open_client", open_client)

    assert gateway.main(["generate", "image", "a dog", "--entry", "flux-schnell"]) == 2

    captured = capsys.readouterr()
    assert not captured.out
    assert "code: GENERATION_KEY_MISSING" in captured.err
    assert "FAL_KEY is required" in captured.err
    assert "fix: astrid generate describe --entry flux-schnell" in captured.err
    assert events == ["client-open", "generation-dispatched", "client-close"]


def test_runtime_connection_stays_open_until_generation_finishes(monkeypatch, capsys):
    events = []
    client = object()

    @contextmanager
    def open_client():
        events.append("connect")
        try:
            yield client
        finally:
            events.append("close")

    def generate(**kwargs):
        assert kwargs["client"] is client
        assert events == ["connect"]
        events.append("complete")
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["dog.png"]})

    monkeypatch.setattr(cli, "_open_client", open_client)
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=generate))
    assert cli.main(["image", "a dog", "--project", "demo"]) == 0
    assert events == ["connect", "complete", "close"]


@pytest.mark.parametrize("align", ["check", "trust"])
def test_explicit_align_is_forwarded_to_the_facade(monkeypatch, align):
    calls = _facade(monkeypatch)
    assert cli.main(["image", "a dog", "--project", "demo", "--align", align]) == 0
    assert calls[0]["align"] == align


def test_runpod_gpu_flags_map_to_ordered_per_job_policy_for_generate(monkeypatch, capsys):
    calls = _facade(monkeypatch)

    assert cli.main(["image", "a dog", "--placement", "runpod",
                     "--gpu", "RTX 4090", "--gpu", "A40",
                     "--offer-digest", "sha256:" + "a" * 64]) == 0

    assert calls[0]["placement"] == {
        "kind": "runpod", "policy": {"gpu_types": ["RTX 4090", "A40"]},
    }
    capsys.readouterr()


def test_runpod_gpu_flags_map_to_ordered_policy_for_target_describe(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(
        describe=lambda **kwargs: (calls.append(kwargs) or {"offer_digest": "digest"})))

    assert cli.main(["describe", "--placement", "runpod", "--gpu", "RTX 4090",
                     "--gpu", "A40", "--json"]) == 0

    assert calls[0]["placement"] == {
        "kind": "runpod", "policy": {"gpu_types": ["RTX 4090", "A40"]},
    }
    assert json.loads(capsys.readouterr().out) == {"offer_digest": "digest"}


def test_runpod_datacenter_override_reaches_generate_and_describe(monkeypatch, capsys):
    calls = _facade(monkeypatch)
    assert cli.main(["image", "a dog", "--placement", "runpod", "--datacenter", "EU-FAKE",
                     "--offer-digest", "sha256:" + "b" * 64]) == 0
    assert calls[0]["datacenter"] == "EU-FAKE"
    capsys.readouterr()

    described = []
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(
        describe=lambda **kwargs: (described.append(kwargs) or {"offer_digest": "digest"})))
    assert cli.main(["describe", "--placement", "runpod", "--datacenter", "EU-FAKE", "--json"]) == 0
    assert described[0]["datacenter"] == "EU-FAKE"
    assert json.loads(capsys.readouterr().out) == {"offer_digest": "digest"}


def test_runpod_datacenter_requires_runpod_and_disclosed_offer(monkeypatch):
    calls = _facade(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["image", "a dog", "--datacenter", "EU-FAKE"])
    with pytest.raises(SystemExit):
        cli.main(["describe", "--datacenter", "EU-FAKE"])
    with pytest.raises(SystemExit):
        cli.main(["image", "a dog", "--placement", "runpod", "--datacenter", "EU-FAKE"])
    assert not calls


def test_runpod_gpu_requires_runpod_target_and_offer_digest(monkeypatch):
    calls = _facade(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["image", "a dog", "--gpu", "RTX 4090"])
    with pytest.raises(SystemExit):
        cli.main(["image", "a dog", "--placement", "runpod", "--gpu", "RTX 4090"])
    assert not calls


def test_runpod_gpu_offer_mismatch_has_actionable_refusal(monkeypatch):
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.sdk.exceptions import CapabilityRuntimeError

    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))

    def reject(**kwargs):
        raise CapabilityRuntimeError("offer or rate changed; disclose it and obtain fresh paid consent")

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=reject))
    with pytest.raises(GenerationReadinessError) as raised:
        cli.main(["image", "a dog", "--placement", "runpod", "--gpu", "RTX 4090",
                  "--offer-digest", "sha256:" + "a" * 64])

    assert "same ordered --gpu values" in str(raised.value)
    assert "GPU type or rate" in str(raised.value)


def test_runpod_generate_without_gpu_keeps_saved_policy_request(monkeypatch, capsys):
    calls = _facade(monkeypatch)

    assert cli.main(["image", "a dog", "--placement", "runpod"]) == 0

    assert calls[0]["placement"] == "runpod"
    capsys.readouterr()


def test_runpod_policy_wait_controls_forward_ceiling_and_capacity_timeout(monkeypatch, capsys):
    calls = _facade(monkeypatch)
    assert cli.main(["image", "a dog", "--placement", "runpod", "--gpu", "RTX 4090",
                     "--consent", "paid", "--max-usd", "2", "--max-hourly-usd", "1.5",
                     "--capacity-timeout", "2m"]) == 0
    assert calls[0]["max_hourly_usd"] == 1.5
    assert calls[0]["capacity_timeout_seconds"] == 120
    assert calls[0]["placement"]["policy"]["gpu_types"] == ["RTX 4090"]
    assert "offer_digest" not in calls[0] and "deadline" not in calls[0]
    capsys.readouterr()


def test_runpod_no_wait_for_capacity_is_independent_of_async_generation(monkeypatch, capsys):
    calls = _facade(monkeypatch)
    assert cli.main(["image", "a dog", "--placement", "runpod", "--no-wait-for-capacity"]) == 0
    assert calls[0]["wait_for_capacity"] is False and "wait" not in calls[0]
    capsys.readouterr()


def test_describe_discloses_the_hourly_waiting_ceiling(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(
        describe=lambda **kwargs: (calls.append(kwargs) or {"waiting_policy": {"max_hourly_usd": kwargs["max_hourly_usd"]}})))
    assert cli.main(["describe", "--placement", "runpod", "--max-hourly-usd", "1.5", "--json"]) == 0
    assert calls[0]["max_hourly_usd"] == 1.5
    assert json.loads(capsys.readouterr().out)["waiting_policy"]["max_hourly_usd"] == 1.5


@pytest.mark.parametrize("modality", ["image", "video"])
def test_installed_console_dispatches_generate_to_the_gateway(monkeypatch, modality):
    from astrid import console
    from astrid.core import gateway

    calls = []
    monkeypatch.setattr(gateway, "main", lambda argv: calls.append(argv) or 0)
    assert console.main(["generate", modality, "a dog", "--placement", "runpod"]) == 0
    assert calls == [["generate", modality, "a dog", "--placement", "runpod"]]


def _public_main(entrypoint):
    from astrid import console
    from astrid.core import gateway

    return {"console": console.main, "gateway": gateway.main}[entrypoint]


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_public_listing_preserves_kind_and_json(monkeypatch, capsys, entrypoint):
    from astrid.core.generation import listing

    calls = []
    rows = [{"id": "example-video", "kind": "video"}]
    monkeypatch.setattr(listing, "generation_rows", lambda kind: calls.append(kind) or rows)
    monkeypatch.setattr(cli, "main", lambda args: pytest.fail("list reached generation parser"))

    assert _public_main(entrypoint)(["generate", "list", "--kind", "video", "--json"]) == 0
    assert calls == ["video"]
    assert json.loads(capsys.readouterr().out) == rows


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
@pytest.mark.parametrize("modality", ["image", "video"])
def test_public_generation_forms_reach_the_existing_facade(monkeypatch, capsys, entrypoint, modality):
    from astrid.core.generation import listing

    calls = _facade(monkeypatch)
    monkeypatch.setattr(listing, "main", lambda args: pytest.fail("generation reached listing parser"))

    # Prompt-only video is refused before connecting; use a reference to
    # continue exercising public forwarding for an admissible video form.
    reference = ["--image-ref", "dog.png"] if modality == "video" else []
    assert _public_main(entrypoint)([
        "generate", modality, "a dog", "--placement", "http://127.0.0.1:8188", "--json", *reference,
    ]) == 0
    assert calls == [{
        "model": None, "placement": "http://127.0.0.1:8188", "dry_run": False,
        "prompt": "a dog", "client": "runtime-client",
        **({"image_ref": "dog.png"} if modality == "video" else {}),
    }]
    assert json.loads(capsys.readouterr().out)["image_paths"] == ["dog.png"]


@pytest.mark.parametrize(("args", "handler"), [
    (["list", "--json"], "listing"),
    (["image", "a dog", "--placement", "managed"], "cli"),
    (["video", "a dog", "--placement", "auto"], "cli"),
])
def test_console_uses_process_arguments_for_each_generate_form(monkeypatch, args, handler):
    import sys
    from astrid import console
    from astrid.core.generation import listing

    calls = []
    modules = {"listing": listing, "cli": cli}
    monkeypatch.setattr(modules[handler], "main", lambda argv: calls.append(argv) or 7)
    other = "cli" if handler == "listing" else "listing"
    monkeypatch.setattr(modules[other], "main", lambda argv: pytest.fail("wrong generate parser"))
    monkeypatch.setattr(sys, "argv", ["astrid", "generate", *args])

    assert console.main() == 7
    assert calls == [args]


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
@pytest.mark.parametrize(("args", "diagnostic"), [
    (["list", "--placement", "managed"], "unrecognized arguments: --placement managed"),
    (["list", "--kind", "model"], "invalid choice: 'model'"),
    (["image", "a dog", "--kind", "image"], "unrecognized arguments: --kind image"),
    (["video", "a dog", "--steps", "many"], "argument --steps: invalid int value"),
    ([], "the following arguments are required: command"),
    (["unknown"], "invalid choice: 'unknown'"),
])
def test_public_generate_rejects_invalid_options_in_the_intended_parser(
    monkeypatch, capsys, entrypoint, args, diagnostic,
):
    from astrid.core.generation import listing

    calls = _facade(monkeypatch)
    monkeypatch.setattr(listing, "generation_rows", lambda kind: pytest.fail("invalid list probed readiness"))

    assert _public_main(entrypoint)(["generate", *args]) == 2
    error = capsys.readouterr().err
    assert diagnostic in error
    if not args or args[0] == "unknown":
        assert "{list}" in error
    assert not calls


def test_generation_help_does_not_load_sdk_or_start_runtime(monkeypatch, capsys):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in {"astrid.sdk.exceptions", "astrid.sdk.client"}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "--help"])
    assert exc.value.code == 0
    assert "--placement" in capsys.readouterr().out


def test_audio_help_lists_runpod_without_excluding_sound_effects(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("help loaded SDK"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("help connected to Runtime"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["audio", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Audio mode: music, tts, or sfx." in help_text
    assert "auto (default), managed, a loopback ComfyUI URL, or runpod." in help_text
    assert "music/tts only" not in help_text


@pytest.mark.parametrize('entrypoint', ['console', 'gateway'])
@pytest.mark.parametrize('status,code', [('ready', 0), ('blocked', 2)])
def test_public_launch_forwards_per_call_controls_without_runtime(
    monkeypatch, capsys, entrypoint, status, code,
):
    from astrid.core.generation.preflight import readiness_envelope
    calls = []
    payload = readiness_envelope(issues=[] if status == 'ready' else [{
        'code': 'target_unreachable', 'severity': 'error', 'message': 'start manually',
        'details': {'manual_start': True, 'reason': 'launch_timeout'}}])
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        launch=lambda **kwargs: calls.append(kwargs) or payload))
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('launch opened Runtime'))
    assert _public_main(entrypoint)(['generate', 'launch', '--engine', 'local',
        '--consent', 'user_env', '--timeout', '0.5', '--json']) == code
    assert calls == [{'placement': 'auto', 'engine': 'local',
                      'consent': ['user_env'], 'timeout': 0.5}]
    assert json.loads(capsys.readouterr().out) == payload


def test_launch_help_is_metadata_only(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_generate_facade', lambda: pytest.fail('help loaded SDK'))
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('help opened Runtime'))
    with pytest.raises(SystemExit) as exc:
        cli.main(['launch', '--help'])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert '--consent' in output and '--timeout' in output


def test_launch_default_does_not_supply_consent(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        launch=lambda **kwargs: calls.append(kwargs) or {'status': 'blocked'}))
    assert cli.main(['launch', '--json']) == 2
    assert calls[0]['consent'] == []


def test_launch_rejects_duplicate_selector_before_sdk(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_generate_facade', lambda: pytest.fail('duplicate reached SDK'))
    assert cli.main(['launch', '--placement', 'auto', '--placement', 'managed', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['issues'][0]['code'] == 'input_rejected'

@pytest.mark.parametrize('mode', ['music', 'tts'])
def test_audio_cli_forwards_runpod_controls_to_facade(monkeypatch, capsys, mode):
    calls = []
    def invoke(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, kernel_task_id='task-a', to_dict=lambda: {'kernel_task_id': 'task-a'})
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(audio=invoke))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext('runtime-client'))
    assert cli.main(['audio', 'hello', '--mode', mode, '--placement', 'runpod', '--consent', 'paid',
        '--max-usd', '2', '--deadline', '2099-01-01T00:00:00Z', '--offer-digest', 'offer',
        '--preparation-seconds', '60', '--execution-seconds', '60', '--idle-seconds', '1800',
        '--recovery-path', 'receipt.json', '--resume', '--no-wait', '--json']) == 0
    assert calls == [{'model': None, 'mode': mode, 'placement': 'runpod', 'prompt': 'hello',
        'dry_run': False, 'client': 'runtime-client', 'wait': False, 'consent': ['paid'],
        'max_usd': 2.0, 'deadline': '2099-01-01T00:00:00Z', 'offer_digest': 'offer',
        'preparation_seconds': 60.0, 'execution_seconds': 60.0, 'idle_seconds': 1800.0,
        'recovery_path': 'receipt.json', 'resume': True}]
    assert json.loads(capsys.readouterr().out)['kernel_task_id'] == 'task-a'


def test_audio_cli_runpod_sound_effects_admits_like_any_slot(monkeypatch, capsys):
    calls = []
    def invoke(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, kernel_task_id='task-a', to_dict=lambda: {'kernel_task_id': 'task-a'})
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(audio=invoke))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext('runtime-client'))
    assert cli.main(['audio', 'waves', '--mode', 'sfx', '--placement', 'runpod', '--consent', 'paid',
        '--max-usd', '2', '--deadline', '2099-01-01T00:00:00Z', '--offer-digest', 'offer',
        '--preparation-seconds', '60', '--execution-seconds', '60', '--idle-seconds', '1800',
        '--recovery-path', 'receipt.json', '--no-wait', '--json']) == 0
    assert calls and calls[0]['mode'] == 'sfx' and calls[0]['placement'] == 'runpod'
    assert json.loads(capsys.readouterr().out)['kernel_task_id'] == 'task-a'


@pytest.mark.parametrize('mode', ['tts', 'music', 'sfx'])
def test_audio_cli_omitted_target_uses_auto(monkeypatch, capsys, mode):
    calls = []
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        audio=lambda **kwargs: calls.append(kwargs) or {'status': 'preview'}))
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('preview connected to Runtime'))
    assert cli.main(['audio', 'hello', '--mode', mode, '--dry-run', '--json']) == 0
    assert calls == [{'model': None, 'mode': mode, 'placement': 'auto',
                      'prompt': 'hello', 'dry_run': True}]
    assert json.loads(capsys.readouterr().out) == {'status': 'preview'}


def test_cli_kind_table_covers_every_generation_kind_and_gates_audio_only_options():
    from astrid.core.generation import entries

    assert set(cli._KIND_SPEC) == set(entries._KINDS)
    parser = cli._parser()
    audio = parser._subparsers._group_actions[0].choices['audio']
    image = parser._subparsers._group_actions[0].choices['image']
    assert cli._KIND_SPEC['audio']['audio_modes'] == ('music', 'tts', 'sfx')
    assert any(action.dest == 'video_ref' for action in audio._actions)
    assert not any(action.dest == 'video_ref' for action in image._actions)
    assert cli._KIND_SPEC['video'].get('prompt_only_guard') is True


@pytest.mark.parametrize(("option", "value", "expected_placement"), [
    ("--gpu", "RTX A6000", {"kind": "runpod", "policy": {"gpu_types": ["RTX A6000"]}}),
    ("--datacenter", "US-TX-3", "runpod"),
])
def test_video_cli_runpod_overrides_accept_recovery_path(monkeypatch, capsys, option, value, expected_placement):
    from astrid.sdk.generation import _video_resolve_mode

    calls = []

    class SDK:
        @staticmethod
        def _infer_video_mode(mode, inputs):
            return mode or "i2v"

    def invoke(**kwargs):
        calls.append(kwargs)
        _video_resolve_mode(SDK, mode=kwargs.get("mode"), inputs={}, resume=False,
                            recovery_path=kwargs.get("recovery_path"), placement=kwargs["placement"])
        return SimpleNamespace(ok=True, to_dict=lambda: {"kernel_task_id": "video-task"})

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(video=invoke))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    argv = ["video", "a moving scene", "--image-ref", "reference.png", "--placement", "runpod",
            "--recovery-path", "receipt.json", "--max-hourly-usd", "2", "--max-usd", "5",
            "--consent", "paid", "--no-wait", "--json", option, value]

    assert cli.main(argv) == 0
    assert calls[0]["placement"] == expected_placement
    assert calls[0]["recovery_path"] == "receipt.json"
    assert json.loads(capsys.readouterr().out) == {"kernel_task_id": "video-task"}


def test_video_recovery_refuses_non_runpod_mapped_placement():
    from astrid.sdk.exceptions import CapabilityValidationError
    from astrid.sdk.generation import _video_resolve_mode

    with pytest.raises(CapabilityValidationError, match="supported only for RunPod"):
        _video_resolve_mode(SimpleNamespace(_infer_video_mode=lambda mode, inputs: "i2v"), mode=None,
                            inputs={}, resume=False, recovery_path="receipt.json",
                            placement={"kind": "local", "policy": {"gpu_types": []}})


def test_workflow_runpod_job_choices_reach_facade_and_validate(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        workflow=lambda **kwargs: calls.append(kwargs) or {'ok': True}))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext('runtime-client'))
    args = ['workflow', 'workflow.json', '--placement', 'runpod', '--gpu', 'RTX 4090',
            '--gpu', 'A40', '--datacenter', 'EU-FAKE', '--offer-digest', 'digest', '--no-wait']
    assert cli.main(args) == 0
    assert calls[0]['placement'] == {'kind': 'runpod', 'policy': {'gpu_types': ['RTX 4090', 'A40']}}
    assert calls[0]['datacenter'] == 'EU-FAKE'
    assert calls[0]['offer_digest'] == 'digest'
    capsys.readouterr()
    for invalid in ([ 'workflow', 'workflow.json', '--gpu', 'A40'],
                    [ 'workflow', 'workflow.json', '--datacenter', 'EU-FAKE'],
                    [ 'workflow', 'workflow.json', '--placement', 'runpod', '--gpu', 'A40']):
        with pytest.raises(SystemExit):
            cli.main(invalid)
    assert len(calls) == 1


def test_runpod_target_without_consent_suggests_waiting_before_exact_offer(capsys):
    for args in (['workflow', 'workflow.json', '--placement', 'runpod', '--gpu', 'A40'],
                 ['image', 'a dog', '--placement', 'runpod', '--gpu', 'A40']):
        with pytest.raises(SystemExit) as stopped:
            cli.main(args)
        assert stopped.value.code == 2
        err = capsys.readouterr().err
        assert err.index('--max-hourly-usd <ceiling>') < err.index('--offer-digest')
        assert 'wait for stock' in err and '--deadline' in err
