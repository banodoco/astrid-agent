"""Public entry metadata and structured input boundaries stay metadata-only."""
from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries


@pytest.mark.parametrize('verb', [['audio', '--mode', 'sfx']])
def test_public_sfx_route_receives_declared_inputs(monkeypatch, tmp_path, capsys, verb):
    from astrid import console
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'synthetic local fixture')
    captured = []
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('dry-run opened Runtime'))
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        audio=lambda **kwargs: captured.append(kwargs) or {'ok': True}))
    assert console.main(['generate', *verb, '--entry', 'audio/hunyuan_video_foley',
                         '--video-ref', str(video), '--prompt', 'gravel footsteps', '--dry-run', '--json']) == 0
    assert captured == [{'entry': 'audio/hunyuan_video_foley', 'mode': 'sfx', 'target': None,
                         'dry_run': True, 'prompt': 'gravel footsteps', 'video_ref': str(video)}]
    assert json.loads(capsys.readouterr().out) == {'ok': True}


def test_foley_help_and_describe_show_license_without_runtime(monkeypatch, capsys):
    from astrid import console
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('metadata opened Runtime'))
    notice = 'Tencent Hunyuan Community license excludes the EU, UK, and South Korea.'
    assert console.main(['generate', 'describe', '--entry', 'audio/hunyuan_video_foley']) == 0
    assert notice in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        cli.main(['audio', '--mode', 'sfx', '--entry', 'audio/hunyuan_video_foley', '--help'])
    assert result.value.code == 0
    assert notice in capsys.readouterr().out


def test_describe_positional_catalog_entry_id(capsys, monkeypatch):
    from astrid import console

    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('metadata opened Runtime'))
    assert console.main(['generate', 'describe', 'audio/ace_step_1_5_t2a_song', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['id'] == 'audio/ace_step_1_5_t2a_song'
    assert result['mode'] == 'music'


def test_describe_unknown_positional_value_reports_path_or_entry(capsys):
    from astrid import console

    assert console.main(['generate', 'describe', 'unknown-generation-entry-xyz']) == 2
    error = capsys.readouterr().err
    assert 'existing workflow path' in error and 'catalog entry ID' in error
    assert 'Errno' not in error


def _description(source="catalog", kind="image", mode="t2i"):
    fields = [{"name": "prompt", "type": "string", "required": True}]
    if mode == "edit":
        fields.append({"name": "image_ref", "type": "string", "required": True})
    else:
        fields.append({"name": "seed", "type": "integer", "required": False, "default": 42})
    if source == "workflow":
        fields.append({"name": "paint_strength", "type": "number", "required": True})
    if source == "action":
        fields = [{"name": "request", "type": "file", "required": True},
                  {"name": "asset_map", "type": "file", "required": True}]
    modes = [mode] if source != "catalog" else ["t2i", "edit"]
    row = {"id": source, "kind": kind, "source": source, "modes": modes,
           "mode": mode, "inputs": fields}
    if source == "action":
        row["action"] = "h3_av.transform"
    return row


@pytest.fixture
def declared(monkeypatch):
    calls = []

    def describe(entry_id, mode=None, **kwargs):
        calls.append((entry_id, mode))
        source = entry_id
        return _description(source, "video" if source == "action" else "image",
                            mode or ("transform" if source == "action" else "t2i"))

    monkeypatch.setattr(entries, "describe_entry", describe)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("opened Runtime before validation"))
    return calls


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_public_describe_uses_only_metadata(monkeypatch, capsys, declared, entrypoint):
    from astrid import console
    from astrid.core import gateway
    monkeypatch.setattr(entries, "resolve_entry_inputs", lambda *a, **k: pytest.fail("describe resolved inputs"))
    main = console.main if entrypoint == "console" else gateway.main
    assert main(["generate", "describe", "--entry", "catalog", "--mode", "edit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _description(mode="edit")
    assert declared == [("catalog", "edit")]


@pytest.mark.parametrize(("source", "kind", "mode"), [
    ("catalog", "image", "edit"), ("workflow", "image", "t2i"), ("action", "video", "transform"),
])
def test_context_help_agrees_with_describe_without_admission(monkeypatch, capsys, declared, source, kind, mode):
    monkeypatch.setattr(entries, "resolve_entry_inputs", lambda *a, **k: pytest.fail("help validated inputs"))
    with pytest.raises(SystemExit) as exc:
        cli.main([kind, "--entry", source, "--mode", mode, "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    projection = _description(source, kind, mode)
    assert f"Entry: {projection['id']} ({source}, {kind})" in help_text
    assert "Modes: " + ", ".join(projection["modes"]) in help_text
    for field in projection["inputs"]:
        assert f"{field['name']} ({field['type']}; " in help_text
    assert declared == [(source, mode)]


def test_json_and_convenience_values_use_shared_resolver(monkeypatch, capsys, tmp_path, declared):
    import astrid
    captured = []
    values = {"prompt": "paint a star", "paint_strength": 0.4}
    file = tmp_path / "inputs.json"
    file.write_text(json.dumps(values))
    expected_description, expected = entries.resolve_entry_inputs("workflow", {**values, "seed": 19},
                                                                 mode="t2i", kind="image")
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("client"))
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        image=lambda **kwargs: captured.append(kwargs) or {"ok": True}))
    assert cli.main(["image", "--entry", "workflow", "--inputs-json", str(file), "--seed", "19", "--json"]) == 0
    assert captured == [{"entry": "workflow", "mode": expected_description["mode"],
                         "target": "auto", "dry_run": False, "client": "client", **expected}]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize(("body", "flags", "reason"), [
    ('{"prompt":"x","prompt":"y"}', [], "duplicate JSON input"),
    ('[]', [], "must contain a JSON object"),
    ('{"prompt":"x","seed":"bad"}', [], "requires an integer"),
    ('{"prompt":"x","seed":true}', [], "requires an integer"),
    ('{"prompt":"x","unknown":1}', [], "unsupported entry inputs"),
    ('{"prompt":"x","seed":NaN}', [], "invalid JSON constant"),
    ('{"prompt":"x"}', ["another prompt"], "duplicate input values"),
    ('{"prompt":"x","seed":1}', ["--seed", "1"], "duplicate input values"),
    ('{"prompt":"x"}', ["--seed", "1", "--seed", "2"], "specified more than once"),
    ('{}', [], "missing required entry inputs"),
])
def test_malformed_unknown_conflicting_inputs_reject_before_runtime(tmp_path, capsys, declared, body, flags, reason):
    file = tmp_path / "inputs.json"
    file.write_text(body)
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", *flags, "--entry", "catalog", "--inputs-json", str(file)])
    assert exc.value.code == 2
    assert reason in capsys.readouterr().err


def test_action_json_reaches_fail_closed_sdk_without_opening_runtime(monkeypatch, capsys, tmp_path, declared):
    import astrid
    from astrid.sdk.exceptions import CapabilityPreconditionError
    from astrid.core.contracts.errors import AstridError
    request, assets = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    file = tmp_path / "inputs.json"
    file.write_text(json.dumps({"request": request, "asset_map": assets}))
    captured = []

    def reject(**kwargs):
        captured.append(kwargs)
        raise CapabilityPreconditionError("tracked parent interface is not yet available")

    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(video=reject))
    assert cli.main(["video", "--entry", "action", "--inputs-json", str(file), '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['issues'][0]['code'] == 'missing_dependency'
    assert 'tracked parent interface' in payload['issues'][0]['message']
    assert len(captured) == 1
    assert captured[0]["request"] == request
    assert "client" not in captured[0]


def test_unspecialized_context_help_shows_each_mode_without_probes(monkeypatch, capsys):
    import socket
    import subprocess

    modes = {mode: _description(mode=mode) for mode in ("t2i", "edit")}
    projection = {**modes["t2i"], "mode": None, "inputs": [], "mode_descriptions": modes}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: projection)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: pytest.fail("help probed target schema"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("help opened Runtime"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("help opened a socket"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("help started a process"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "--entry", "catalog", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "Inputs for mode t2i:" in help_text
    assert "Inputs for mode edit:" in help_text
    assert "seed (integer; optional, default=42)" in help_text
    assert "image_ref (string; required)" in help_text


def test_conflicting_selectors_reject_before_metadata_or_runtime(capsys, declared):
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "hello", "--entry", "catalog", "--model", "different"])
    assert exc.value.code == 2
    assert "select different generation entries" in capsys.readouterr().err
    assert not declared


def test_context_help_exposes_declared_aliases_and_constraints(monkeypatch, capsys):
    projection = _description(source="workflow")
    field = projection["inputs"][-1]
    field.update(aliases=["texture"], min=0, max=1)
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: projection)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("help opened Runtime"))
    with pytest.raises(SystemExit) as result:
        cli.main(["image", "--entry", "workflow", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "Aliases: texture" in help_text
    assert "min: 0" in help_text and "max: 1" in help_text


@pytest.mark.parametrize('prompt_args', [['Hello from Astrid.'], ['--prompt', 'Hello from Astrid.']])
def test_real_tts_gateway_maps_prompt_without_runtime(monkeypatch, capsys, prompt_args):
    from astrid.core import gateway
    captured = []
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('dry-run opened Runtime'))
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        audio=lambda **kwargs: captured.append(kwargs) or {'ok': True}))
    assert gateway.main(['generate', 'audio', *prompt_args, '--mode', 'tts',
                         '--entry', 'audio/qwen3_tts_custom_voice', '--dry-run', '--json']) == 0
    assert captured[0]['text'] == 'Hello from Astrid.'
    assert 'prompt' not in captured[0]


@pytest.mark.parametrize('text', ['hello', 'different'])
def test_real_tts_duplicate_prompt_names_declared_input(text):
    with pytest.raises(ValueError, match='--input text='):
        entries.resolve_entry_inputs('audio/qwen3_tts_custom_voice',
                                     {'prompt': 'hello', 'text': text}, mode='tts', kind='audio')


@pytest.mark.parametrize(('kind', 'mode', 'ports', 'values', 'expected'), [
    ('image', 't2i', [{'name': 'prompt', 'type': 'string'}], {'prompt': 'x'}, {'prompt': 'x'}),
    ('audio', 'tts', [{'name': 'prompt', 'type': 'string'}, {'name': 'text', 'type': 'string'}],
     {'prompt': 'x'}, {'prompt': 'x'}),
    ('audio', 'tts', [{'name': 'text', 'type': 'string', 'aliases': ['prompt']}],
     {'prompt': 'x'}, {'text': 'x'}),
    ('audio', 'tts', [{'name': 'text', 'type': 'string'}, {'name': 'negative_prompt', 'type': 'string'}],
     {'negative_prompt': 'x'}, {'negative_prompt': 'x'}),
])
def test_prompt_mapping_preserves_declared_contract(monkeypatch, kind, mode, ports, values, expected):
    monkeypatch.setattr(entries, 'describe_entry', lambda *a, **k: {
        'id': 'fixture', 'source': 'workflow', 'kind': kind, 'mode': mode,
        'modes': [mode], 'inputs': ports})
    assert entries.resolve_entry_inputs('fixture', values, mode=mode)[1] == expected


@pytest.mark.parametrize('ports', [
    [{'name': 'words', 'type': 'string'}, {'name': 'script', 'type': 'string'}],
    [{'name': 'text', 'type': 'integer'}],
])
def test_tts_prompt_without_canonical_text_has_declared_input_hint(monkeypatch, ports):
    monkeypatch.setattr(entries, 'describe_entry', lambda *a, **k: {
        'id': 'fixture', 'source': 'workflow', 'kind': 'audio', 'mode': 'tts',
        'modes': ['tts'], 'inputs': ports})
    with pytest.raises(ValueError, match='use declared --input NAME=VALUE'):
        entries.resolve_entry_inputs('fixture', {'prompt': 'hello'}, mode='tts')


# Measured 120 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_real_named_tts_sdk_maps_prompt_and_refuses_duplicate_before_target(monkeypatch):
    import astrid.sdk.generation as sdk_generation
    from astrid.sdk.generation import GenerationInputError

    captured = []
    monkeypatch.setattr(sdk_generation.GenerationFacade, '_generate_on_target',
                        lambda self, **kwargs: captured.append(kwargs) or {'ok': True})
    facade = sdk_generation.GenerationFacade()
    assert facade.audio(entry='audio/qwen3_tts_custom_voice', mode='tts',
                        prompt='Hello from Astrid.', placement='local', dry_run=True) == {'ok': True}
    assert captured[0]['inputs'] == {'text': 'Hello from Astrid.'}
    for text in ('Hello from Astrid.', 'different'):
        with pytest.raises(GenerationInputError, match='--input text='):
            facade.audio(entry='audio/qwen3_tts_custom_voice', mode='tts',
                         prompt='Hello from Astrid.', text=text, placement='local', dry_run=True)
    assert len(captured) == 1
