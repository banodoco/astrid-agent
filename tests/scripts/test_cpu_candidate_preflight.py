"""Unit coverage for the CPU preflight gate; all external work is faked."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import preflight
from scripts.catalog_dry_run import catalog_dry_run


class FakeCommand:
    def __init__(self, argv, *, output="", status=0, log_path="fake.log"):
        self.command = list(map(str, argv))
        self.cwd = "/fake"
        self.exit_status = status
        self.log_path = log_path
        self.output = output

    def record(self):
        return {"command": self.command, "cwd": self.cwd, "exit_status": self.exit_status,
                "log_path": self.log_path}


class FakeRunner:
    def __init__(self, responses=None, logs=None):
        self.responses = responses or {}
        self.logs = Path(logs or "/fake/logs")
        self.commands = []

    def __call__(self, argv, **kwargs):
        argv = list(map(str, argv))
        if "generate" in argv and "--dry-run" not in argv:
            raise AssertionError("unsafe generation invocation")
        key = tuple(argv)
        result = self.responses.get(key, FakeCommand(argv))
        self.commands.append(result)
        return result

    def save(self, argv, cwd, rc, output):
        result = FakeCommand(argv, output=output, status=rc)
        self.commands.append(result)
        return result


def cell(name="image", template="flux", sources=None):
    return preflight.Cell(name, "entry", "t2i", template, sources=list(sources or []))


def test_map_vibe_results_maps_declared_and_sparse_parameters_and_statuses():
    cells = [cell("image", "image/flux")]
    selected = [
        "tests/test_ready_templates.py::test_schema[image/flux]",
        "tests/test_ready_templates.py::test_canonical_first_build[declared-image/flux]",
        "tests/test_ready_templates.py::test_canonical_first_build[sparse-image/flux]",
    ]
    results = {
        selected[0]: {"nodeid": selected[0], "status": "PASS", "reason": ""},
        selected[1]: {"nodeid": selected[1], "status": "PASS", "reason": ""},
        selected[2]: {"nodeid": selected[2], "status": "FAIL", "reason": "bad sparse rebuild"},
    }
    command = FakeCommand(["pytest"])
    preflight.map_vibe_results(cells, selected, results, command)
    assert cells[0].checks["schema"]["status"] == "PASS"
    assert cells[0].checks["rebuild"]["status"] == "FAIL"
    assert len(cells[0].checks["rebuild"]["tests"]) == 2


def test_select_cells_reads_authoritative_live_json_and_keeps_ready_ids(monkeypatch, tmp_path):
    declarations = [
        {"cell": "catalog-cell", "kind": "image", "entry": "catalog-image", "mode": "t2i"},
        {"cell": "ready-cell", "kind": "audio", "entry": "audio/ready-template", "mode": "tts"},
    ]
    source = tmp_path / "live_cells.json"
    source.write_text(json.dumps({"cells": declarations}))
    monkeypatch.setattr(preflight, "LIVE_CELLS", source)
    catalog = SimpleNamespace(id="catalog-image", modality="image",
                              modes={"t2i": SimpleNamespace(backends={"local": SimpleNamespace(template="image/catalog")})})

    class Registry:
        def get(self, identifier):
            if identifier == catalog.id:
                return catalog
            raise KeyError(identifier)

        def get_by_mode(self, identifier, mode):
            assert (identifier, mode) == (catalog.id, "t2i")
            return catalog, catalog.modes[mode]

        def list_all(self, include_closed=True):
            return [catalog, SimpleNamespace(id="other", modality="image",
                                             modes={"i2i": SimpleNamespace(backends={})})]

    registry = Registry()
    selected = preflight.select_cells(registry)
    assert [(item.name, item.entry, item.mode, item.template) for item in selected] == [
        ("catalog-cell", "catalog-image", "t2i", "image/catalog"),
        ("ready-cell", "audio/ready-template", "tts", "audio/ready-template"),
    ]
    assert [item.name for item in selected] == [row["cell"] for row in declarations]
    all_cells = preflight.select_cells(registry, all_catalog=True)
    assert [(item.name, item.entry, item.mode) for item in all_cells] == [
        ("catalog-cell", "catalog-image", "t2i"),
        ("ready-cell", "audio/ready-template", "tts"),
        ("other/i2i", "other", "i2i"),
    ]


def test_live_cell_set_matches_authoritative_json_file():
    from astrid.core.model_catalog.registry import ModelRegistry

    declarations = preflight.live_cells()
    cells = preflight.select_cells(ModelRegistry.load_default())
    assert [(cell.name, cell.kind, cell.entry, cell.mode) for cell in cells] == [
        (row["cell"], row["kind"], row["entry"], row["mode"]) for row in declarations]


def test_map_vibe_results_keeps_recipe_pin_pass_and_fail_and_unverifies_only_missing_check():
    target = "tests/test_ready_templates.py::test_schema[image/flux]"
    c = cell(template="image/flux")
    command = FakeCommand(["pytest"])
    preflight.map_vibe_results([c], [target], {target: {"nodeid": target, "status": "PASS",
            "reason": ""}}, command)
    assert c.checks["schema"]["status"] == "PASS"
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [target], {target: {"nodeid": target, "status": "FAIL",
            "reason": "no object_info captured for comfy_core at recipe pin abc"}}, command)
    assert c.checks["schema"]["status"] == "FAIL"
    assert f"{target}: no object_info captured for comfy_core at recipe pin abc" == c.checks["schema"]["reason"]
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [target], {target: {"nodeid": target, "status": "FAIL",
            "reason": "schema input type mismatch"}}, command)
    assert c.checks["schema"]["status"] == "FAIL"
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [], {}, command)
    assert c.checks["schema"]["status"] == "UNVERIFIED"


def test_map_vibe_results_marks_selected_test_without_result_fail():
    target = "tests/test_ready_templates.py::test_schema[image/flux]"
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [target], {}, FakeCommand(["pytest"]))
    assert c.checks["schema"]["status"] == "FAIL"
    assert "selected test produced no result" in c.checks["schema"]["reason"]


def test_template_sources_include_authored_metadata_without_typed_refs():
    workflow = SimpleNamespace(
        metadata={"custom_node_packs": {"ComfyUI-QwenTTS": {"commit": "a" * 40}}},
        requirements=SimpleNamespace(custom_nodes=[], custom_node_refs=[]))
    assert preflight.template_source_slugs(workflow) == ["ComfyUI-QwenTTS"]
    workflow.requirements.custom_node_refs = [{"slug": "ComfyUI-VideoHelperSuite", "source": "git"}]
    assert preflight.template_source_slugs(workflow) == ["ComfyUI-QwenTTS", "ComfyUI-VideoHelperSuite"]


def test_check_vibe_head_mismatch_refuses_pytest(tmp_path):
    c = cell()
    runner = FakeRunner()
    checkout = tmp_path / "vibe"
    checkout.mkdir()
    runner.responses[tuple(["git", "rev-parse", "HEAD"])] = FakeCommand(["git", "rev-parse", "HEAD"], output="wrong")
    preflight.check_vibe([c], runner, checkout, None, {}, "required")
    assert c.checks["schema"]["status"] == "FAIL"
    assert not any("pytest" in cmd.command for cmd in runner.commands)


def test_check_vibe_runs_declared_cases_and_reads_source_ownership(tmp_path):
    checkout = tmp_path / "vibe"
    checkout.mkdir()
    schema = "test_ready_template_offline_schemas_by_identity[audio/tts]"
    rebuild = "test_ready_template_canonical_first_build_matches_admitted_candidate"
    names = [schema, f"{rebuild}[declared-audio/tts]", f"{rebuild}[sparse-audio/tts]"]
    ids = ["tests/test_ready_templates.py::" + name for name in names]
    class VibeRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            argv = list(map(str, argv))
            output = ""
            if argv == ["git", "rev-parse", "HEAD"]:
                output = "pinned"
            elif "--collect-only" in argv:
                output = "\n".join(ids)
            elif any(arg.startswith("--junitxml=") for arg in argv):
                report = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--junitxml=")))
                report.write_text('<testsuites><testsuite>' + ''.join(
                    f'<testcase classname="tests.test_ready_templates" name="{name}">'
                    + ('<failure message="legacy schema failure"/>' if name == schema else '')
                    + '</testcase>' for name in names) + '</testsuite></testsuites>')
            elif "-c" in argv:
                output = json.dumps({"audio/tts": ["ComfyUI-QwenTTS"], "audio/absent": []})
            result = FakeCommand(argv, output=output)
            self.commands.append(result)
            return result

    runner = VibeRunner(logs=tmp_path)
    c = cell("tts", "audio/tts")
    undeclared = cell("undeclared", "audio/absent")
    pins = {"comfy_core": "core-pin"}
    assert preflight.check_vibe([c, undeclared], runner, checkout, Path("/vibe/python"), pins, "pinned") is True
    assert c.checks["schema"]["status"] == "FAIL"
    assert "legacy schema failure" in c.checks["schema"]["reason"]
    assert c.checks["rebuild"]["status"] == "PASS"
    assert c.sources == ["comfyui-qwentts"]
    assert undeclared.checks["schema"]["status"] == "UNVERIFIED"
    assert undeclared.checks["rebuild"]["status"] == "UNVERIFIED"
    source_query = next(command for command in runner.commands if "-c" in command.command)
    assert json.loads(source_query.command[-1]) == ["audio/absent", "audio/tts"]
    assert json.loads((tmp_path / "pack-identity-commits.json").read_text()) == pins
    executed = next(command for command in runner.commands if "-rA" in command.command)
    assert all(nodeid in executed.command for nodeid in ids)
    assert "--pack-identity-commits=" + str(tmp_path / "pack-identity-commits.json") in executed.command


def test_run_gate_harness_exception_fails_every_check(monkeypatch, tmp_path):
    c = cell()
    runner = FakeRunner()
    monkeypatch.setattr(preflight, "require_success", lambda command: (_ for _ in ()).throw(RuntimeError("boom")))
    report = preflight.run_gate([c], object(), runner, SimpleNamespace())
    assert report["ok"] is False
    assert {key: value["status"] for key, value in c.checks.items()} == {key: "FAIL" for key in preflight.CHECKS}
    assert "preflight harness crashed" in c.checks["catalog"]["reason"]


def test_constructor_checks_reject_transitive_lock_drift_only_for_source_owners(tmp_path):
    lock = tmp_path / "engine.lock"
    lock.write_text("\n".join(f"{name}==1.0" for name in preflight.ENGINE_PACKAGES)
                    + "\nfilelock==4.2.5\n"
                    + "\n".join(f"{name} @ https://example.test/{name}-2.0.0%2Bcu130.whl"
                                 for name in preflight.CPU_WHEELS))
    installed = {name: "1.0" for name in preflight.ENGINE_PACKAGES}
    installed.update(torch="2.0.0+cpu", torchaudio="2.0.0+cpu", filelock="4.2.4")

    class EngineRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            assert "-c" in argv  # Version inspection happens before any test or source fetch.
            result = FakeCommand(argv, output=json.dumps(installed))
            self.commands.append(result)
            return result

    owner = cell("source-owner", sources=["comfyui-qwentts"])
    other = cell("other")
    runner = EngineRunner(logs=tmp_path)
    preflight.check_constructors([owner, other], runner, tmp_path / "venv", tmp_path, lock, None, {})
    assert owner.checks["constructor"]["status"] == "FAIL"
    assert "filelock: requires 4.2.5, installed 4.2.4" in owner.checks["constructor"]["reason"]
    assert other.checks["constructor"]["status"] == "UNVERIFIED"
    assert len(runner.commands) == 1


def test_runner_refuses_generate_without_dry_run(tmp_path):
    with pytest.raises(ValueError, match="forbids generation"):
        preflight.Runner(tmp_path)(["astrid", "generate", "image"])


def test_catalog_probe_refuses_a_request_builder_that_drops_dry_run(monkeypatch, tmp_path):
    from astrid.core.model_catalog import requests

    monkeypatch.setattr(requests, "build_generate_argv", lambda *a, **k: ["generate", "image", "--consent", "paid"])
    entry = SimpleNamespace(id="sample", modality="image", modes={"t2i": SimpleNamespace(requires=())})
    calls = []
    with pytest.raises(ValueError, match="--dry-run"):
        catalog_dry_run(entry, "t2i", tmp_path, invoke=lambda argv: calls.append(argv))
    assert calls == []


@pytest.mark.parametrize("stdout,rc,want", [
    ('{"ok": true}', 0, ""),
    ('{"ok": false}', 0, "{"),
    ("not-json", 0, "CLI did not return JSON"),
])
def test_catalog_probe_reports_pass_fail_unverified_shape(stdout, rc, want, tmp_path):
    entry = SimpleNamespace(id="sample", modality="image", modes={"t2i": SimpleNamespace(requires=("prompt", "image_ref", "seed"))})
    calls = []

    def invoke(argv):
        calls.append(argv)
        print(stdout)
        return rc

    result = catalog_dry_run(entry, "t2i", tmp_path, invoke=invoke)
    assert (not result.failure) == (not want)
    if want:
        assert want in result.failure
    assert len(calls) == 1
    argv = calls[0]
    assert "--dry-run" in argv
    assert "--edit" in argv and "--input" in argv
    assert "generate" in argv


def test_main_json_report_has_stable_top_level_shape(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    for name in list(sys.modules):
        if name == "vibecomfy" or name.startswith("vibecomfy."):
            monkeypatch.delitem(sys.modules, name)
    checkout = tmp_path / "pinned-vibecomfy"
    vibe_python = tmp_path / "vibe-venv/bin/python"
    vibe_python.parent.mkdir(parents=True)
    vibe_python.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("ASTRID_VIBECOMFY_CHECKOUT", str(checkout))
    fake_registry = SimpleNamespace(load_default=lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "astrid.core.model_catalog.registry",
                        SimpleNamespace(ModelRegistry=fake_registry))
    monkeypatch.setattr(preflight, "select_cells", lambda *a, **k: [cell()])
    lock = tmp_path / "engine.lock"
    lock.write_text("fixture lock")
    recipe = SimpleNamespace(environments=[SimpleNamespace(name="cpu", requirements=SimpleNamespace(packaged_lock=SimpleNamespace(path="fixture.lock")))],
                             primary_environment="cpu")
    sources = {name: SimpleNamespace(pin=SimpleNamespace(revision=f"{name}-revision"))
               for name in preflight.SOURCE_SLUGS}
    monkeypatch.setattr(preflight, "recipe_facts", lambda: (recipe, sources, {"comfy_core": "core-rev"}, lock, "vibe-rev"))
    def fake_catalog(cells, registry, runner, stage_controls=None):
        assert sys.path[0] == str(checkout)
        assert os.environ["ASTRID_VIBECOMFY_CHECKOUT"] == str(checkout)
        for c in cells:
            c.checks["catalog"] = preflight.outcome("PASS", commands=[runner.save(["catalog", "--dry-run"], "/fake", 0, "{}")])

    def fake_vibe(cells, runner, selected_checkout, *args):
        assert selected_checkout == checkout
        assert args[0] == vibe_python
        for c in cells:
            c.checks.update(schema=preflight.outcome("UNVERIFIED", "no declared schema test"),
                            rebuild=preflight.outcome("PASS", commands=[FakeCommand(["pytest", "rebuild"])]))
        return True

    monkeypatch.setattr(preflight, "check_catalog", fake_catalog)
    monkeypatch.setattr(preflight, "check_vibe", fake_vibe)
    def fake_constructors(cells, *args):
        for c in cells:
            c.checks["constructor"] = preflight.outcome("PASS", commands=[FakeCommand(["pytest", "engine_deps"])])
        return {"torch": "2.0+cpu"}

    monkeypatch.setattr(preflight, "check_constructors", fake_constructors)
    output = tmp_path / "report.json"
    assert preflight.main(["--json", str(output), "--cache-dir", "cache",
                           "--vibecomfy-checkout", str(checkout),
                           "--vibecomfy-python", str(vibe_python)]) == 0
    payload = json.loads(output.read_text())
    assert payload["schema"] == "astrid.cpu-preflight.v1"
    assert payload["candidate"]["vibecomfy_revision"] == "vibe-rev"
    assert payload["candidate"]["pack_identity_commits"] == {"comfy_core": "core-rev"}
    assert payload["candidate"]["engine_lock_sha256"]
    assert payload["engine_versions"] == {"torch": "2.0+cpu"}
    assert payload["ok"] is True  # UNVERIFIED does not block the candidate.
    row = payload["cells"][0]
    assert {kind: row["checks"][kind]["status"] for kind in preflight.CHECKS} == {
        "catalog": "PASS", "schema": "UNVERIFIED", "rebuild": "PASS", "constructor": "PASS"}
    assert row["checks"]["catalog"]["commands"][0]["command"] == ["catalog", "--dry-run"]
    assert row["checks"]["rebuild"]["commands"][0]["command"] == ["pytest", "rebuild"]
    assert len(payload["commands"]) == 3  # candidate HEAD, worktree status, catalog probe
    assert payload["commands"][0]["command"] == ["git", "rev-parse", "HEAD"]
    assert all(command["exit_status"] == 0 for command in payload["commands"])
    assert all(Path(command["log_path"]).is_absolute() for command in payload["commands"])


def test_catalog_dry_run_converts_system_exit_to_report(monkeypatch, tmp_path):
    entry = SimpleNamespace(id="sample", modality="image", modes={"t2i": SimpleNamespace(requires=())})

    def invoke(argv):
        raise SystemExit(3)

    result = catalog_dry_run(entry, "t2i", tmp_path, invoke=invoke)
    assert result.exit_status == 3
    assert result.failure


def test_main_registry_crash_reports_every_live_cell(monkeypatch, tmp_path):
    def crash():
        raise RuntimeError("catalog load crashed")

    monkeypatch.setitem(sys.modules, "astrid.core.model_catalog.registry",
                        SimpleNamespace(ModelRegistry=SimpleNamespace(load_default=crash)))
    monkeypatch.setattr(preflight, "live_cells", lambda: [
        {"cell": "tts", "kind": "audio", "entry": "audio/tts", "mode": "tts"},
        {"cell": "t2i", "kind": "image", "entry": "image/t2i", "mode": "t2i"},
    ])
    output = tmp_path / "crash.json"
    assert preflight.main(["--json", str(output), "--cache-dir", str(tmp_path / "cache")]) == 1
    report = json.loads(output.read_text())
    assert set(report) == set(preflight.new_report())
    assert {row["name"] for row in report["cells"]} == {"tts", "t2i"}
    assert all(check["status"] == "FAIL" for row in report["cells"] for check in row["checks"].values())
    assert report["commands"][0]["exit_status"] == -1
    assert "catalog load crashed" in Path(report["commands"][0]["log_path"]).read_text()


def test_catalog_dry_run_builds_required_inputs_for_public_cli(tmp_path):
    entry = SimpleNamespace(id="audio-demo", modality="audio", modes={"tts": SimpleNamespace(requires=("prompt", "video_ref", "voice"))})
    calls = []
    result = catalog_dry_run(entry, "tts", tmp_path, invoke=lambda argv: calls.append(argv) or (print('{"ok": true}') or 0))
    assert not result.failure
    argv = calls[0]
    assert argv[:3] == ["generate", "audio", "A clear test scene"]
    assert "--dry-run" in argv and "--json" in argv
    assert "--input" in argv and "voice=test-value" in argv
    media = Path(argv[argv.index("--video-ref") + 1])
    assert media.read_bytes() == b"test image"


def test_check_catalog_reports_managed_ready_template_declaration_error(monkeypatch, tmp_path):
    from astrid.core.generation import managed_preparation
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.core.model_catalog.registry import ModelRegistry

    def missing_declarations(*_args, **_kwargs):
        raise IntegrityError("managed model has no declared digest: fixture.safetensors")

    monkeypatch.setattr(managed_preparation, "judge_declarations", missing_declarations)
    controls_file = tmp_path / "controls.json"
    controls_file.write_text(json.dumps({"max_usd": 1, "deadline": "2099-01-01T00:00:00Z"}))
    c = preflight.Cell("z-image", "z-image", "t2i", "image/z_image_turbo", kind="image")

    preflight.check_catalog([c], ModelRegistry.load_default(), FakeRunner(logs=tmp_path), controls_file)

    assert c.checks["catalog"]["status"] == "FAIL"
    assert "managed model has no declared digest: fixture.safetensors" in c.checks["catalog"]["reason"]


def test_check_catalog_requires_stage_controls_and_consumes_exact_values(monkeypatch, tmp_path):
    class Registry:
        def get(self, identifier):
            raise KeyError(identifier)

    c = cell()
    runner = FakeRunner()
    monkeypatch.setattr(preflight, "catalog_dry_run", lambda *a, **k: pytest.fail("must not generate without controls"))
    preflight.check_catalog([c], Registry(), runner)
    assert c.checks["catalog"]["status"] == "UNVERIFIED"
    assert "requires --stage-controls" in c.checks["catalog"]["reason"]
    assert not any("generate" in command.command for command in runner.commands)
    monkeypatch.setattr(preflight, "catalog_dry_run", catalog_dry_run)

    controls_file = tmp_path / "controls.json"
    controls_file.write_text(json.dumps({"max_usd": 3.25, "deadline": "2099-02-03T04:05:06Z"}))
    declarations = preflight.live_cells()
    monkeypatch.setattr(preflight, "live_cells", lambda: declarations)
    by_id = {row["entry"]: row["kind"] for row in declarations}

    def describe(identifier, mode, registry, _native):
        return {"kind": by_id[identifier], "inputs": [
            {"name": "prompt", "required": True, "type": "string"}]}

    monkeypatch.setattr("astrid.core.generation.entries.describe_entry", describe)
    from astrid import console
    seen_argv = []

    def invoke(argv):
        seen_argv.append(list(argv))
        print('{"ok": true}')
        return 0

    monkeypatch.setattr(console, "main", invoke)
    cells = [preflight.Cell(row["cell"], row["entry"], row["mode"], row["entry"], kind=row["kind"])
             for row in declarations]
    preflight.check_catalog(cells, Registry(), runner, controls_file)
    assert all(c.checks["catalog"]["status"] == "PASS" for c in cells)
    assert len(seen_argv) == len(declarations)
    for argv, declaration in zip(seen_argv, declarations):
        assert argv[0:2] == ["generate", declaration["kind"]]
        assert argv[argv.index("--entry") + 1] == declaration["entry"]
        assert "--dry-run" in argv
        assert argv[argv.index("--max-usd") + 1] == "3.25"
        assert argv[argv.index("--deadline") + 1] == "2099-02-03T04:05:06Z"
    assert all("--dry-run" in command.command for command in runner.commands if "generate" in command.command)


def test_ready_request_entry_uses_native_descriptor_metadata(monkeypatch):
    from scripts.catalog_dry_run import ready_request_entry

    descriptor = {"kind": "image", "inputs": [
        {"name": "prompt", "required": True, "type": "string"},
        {"name": "seed", "required": True, "type": "integer", "min": 9},
        {"name": "optional", "required": False, "type": "string"},
    ]}
    monkeypatch.setattr("astrid.core.generation.entries.describe_entry",
                        lambda identifier, mode, registry, _native: descriptor)
    entry = ready_request_entry("image/ready", "t2i", "image", object())
    assert entry.id == "image/ready"
    assert entry.modality == "image"
    assert entry.modes["t2i"].requires == ("prompt", "seed")
    assert entry.input_fields["seed"]["min"] == 9


def test_ready_required_media_uses_existing_file_even_with_widget_default(tmp_path):
    from scripts.catalog_dry_run import required_value

    for port in ({"type": "file", "default": "missing-video.mp4"},
                 {"type": "string", "media_semantics": "image", "default": ""},
                 {"type": "string", "aliases": ["image_ref"], "default": ""}):
        value = required_value("media", tmp_path, port)
        assert Path(value).is_file()
