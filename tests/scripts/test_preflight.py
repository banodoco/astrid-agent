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
    preflight.map_vibe_results(cells, selected, results, {}, command, {})
    assert cells[0].checks["schema"]["status"] == "PASS"
    assert cells[0].checks["rebuild"]["status"] == "FAIL"
    assert len(cells[0].checks["rebuild"]["tests"]) == 2


def test_select_cells_maps_defaults_and_deduplicates_catalog_templates(monkeypatch):
    class Candidate:
        def __init__(self, kind, mode, entry, priority):
            self.kind, self.mode, self.entry, self.priority = kind, mode, entry, priority

    from astrid.core.model_catalog.registry import ModelRegistry

    candidates = [Candidate("image", "t2i", "image/flux", 4), Candidate("image", "t2i", "other", 1)]
    monkeypatch.setattr("astrid.core.generation.defaults.pack_candidates", lambda: candidates)
    spec = SimpleNamespace(backends={"local": SimpleNamespace(template="image/flux")})
    extra = SimpleNamespace(backends={"local": SimpleNamespace(template="image/extra")})
    entries = [SimpleNamespace(id="flux-entry", modes={"t2i": spec}),
               SimpleNamespace(id="extra-entry", modes={"t2i": extra})]

    registry = ModelRegistry(entries)
    selected = preflight.select_cells(registry)
    assert [(item.entry, item.mode, item.template) for item in selected] == [("flux-entry", "t2i", "image/flux")]
    all_cells = preflight.select_cells(registry, all_catalog=True)
    assert len(all_cells) == 2
    assert {item.template for item in all_cells} == {"image/flux", "image/extra"}


def test_map_vibe_results_quarantines_unverified_but_recipe_core_identity_stays_fail():
    target = "tests/test_ready_templates.py::test_schema[image/flux]"
    c = cell(template="image/flux")
    command = FakeCommand(["pytest"])
    quarantine = {target: "offline owner metadata absent"}
    preflight.map_vibe_results([c], [target], {target: {"nodeid": target, "status": "FAIL",
            "reason": "legacy failure"}}, quarantine, command, {})
    assert c.checks["schema"]["status"] == "UNVERIFIED"
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [target], {target: {"nodeid": target, "status": "FAIL",
            "reason": "no object_info captured for comfy_core at abc"}}, quarantine,
            command, {"comfy_core": "abc"})
    assert c.checks["schema"]["status"] == "FAIL"


def test_map_vibe_results_marks_selected_test_without_result_fail():
    target = "tests/test_ready_templates.py::test_schema[image/flux]"
    c = cell(template="image/flux")
    preflight.map_vibe_results([c], [target], {}, {}, FakeCommand(["pytest"]), {})
    assert c.checks["schema"]["status"] == "FAIL"
    assert "selected test produced no result" in c.checks["schema"]["reason"]


def test_check_vibe_head_mismatch_refuses_pytest(tmp_path):
    c = cell()
    runner = FakeRunner()
    checkout = tmp_path / "vibe"
    checkout.mkdir()
    runner.responses[tuple(["git", "rev-parse", "HEAD"])] = FakeCommand(["git", "rev-parse", "HEAD"], output="wrong")
    preflight.check_vibe([c], runner, checkout, None, {}, "required")
    assert c.checks["schema"]["status"] == "FAIL"
    assert not any("pytest" in cmd.command for cmd in runner.commands)


def test_run_gate_harness_exception_fails_every_check(monkeypatch, tmp_path):
    c = cell()
    runner = FakeRunner()
    monkeypatch.setattr(preflight, "require_success", lambda command: (_ for _ in ()).throw(RuntimeError("boom")))
    report = preflight.run_gate([c], object(), runner, SimpleNamespace())
    assert report["ok"] is False
    assert {key: value["status"] for key, value in c.checks.items()} == {key: "FAIL" for key in preflight.CHECKS}
    assert "preflight harness crashed" in c.checks["catalog"]["reason"]


def test_runner_refuses_generate_without_dry_run(tmp_path):
    with pytest.raises(ValueError, match="forbids generation"):
        preflight.Runner(tmp_path)(["astrid", "generate", "image"])


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
    checkout = tmp_path / "pinned-vibecomfy"
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
    def fake_catalog(cells, registry, runner):
        assert sys.path[0] == str(checkout)
        assert os.environ["ASTRID_VIBECOMFY_CHECKOUT"] == str(checkout)
        for c in cells:
            c.checks["catalog"] = preflight.outcome("PASS", commands=[runner.save(["catalog", "--dry-run"], "/fake", 0, "{}")])

    def fake_vibe(cells, runner, selected_checkout, *args):
        assert selected_checkout == checkout
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
    assert preflight.main(["--json", str(output), "--cache-dir", str(tmp_path / "cache"),
                           "--vibecomfy-checkout", str(checkout)]) == 0
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


def test_catalog_dry_run_converts_system_exit_to_report(monkeypatch, tmp_path):
    entry = SimpleNamespace(id="sample", modality="image", modes={"t2i": SimpleNamespace(requires=())})

    def invoke(argv):
        raise SystemExit(3)

    result = catalog_dry_run(entry, "t2i", tmp_path, invoke=invoke)
    assert result.exit_status == 3
    assert result.failure


def test_catalog_dry_run_builds_required_inputs_for_public_cli(tmp_path):
    entry = SimpleNamespace(id="audio-demo", modality="audio", modes={"tts": SimpleNamespace(requires=("prompt", "audio_ref", "voice"))})
    calls = []
    result = catalog_dry_run(entry, "tts", tmp_path, invoke=lambda argv: calls.append(argv) or (print('{"ok": true}') or 0))
    assert not result.failure
    argv = calls[0]
    assert argv[:3] == ["generate", "audio", "A clear test scene"]
    assert "--dry-run" in argv and "--json" in argv
    assert "--input" in argv and "voice=test-value" in argv
    media = Path(argv[argv.index("--input") + 1].split("=", 1)[1])
    assert media.read_bytes() == b"test media"
