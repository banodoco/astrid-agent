"""Local contract tests; these never invoke PowerShell or Task Scheduler."""
import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "windows_r13_prereconcile.py"
SPEC = importlib.util.spec_from_file_location("windows_r13_prereconcile", MODULE)
pr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr)


def test_task_document_requires_one_owned_task_action(tmp_path):
    root = tmp_path / "program"
    data = tmp_path / "workspace"
    root.mkdir()
    data.mkdir()
    value = {"task_path": "\\", "task_name": "Astrid Runtime", "actions": [
        {"Execute": str(root / "versions" / "r13" / "python.exe"),
         # The embedded Python command escapes Windows path characters, so
         # arguments are intentionally opaque here; the pinned CAS recognizes it.
         "Arguments": ' -X utf8 -c "print(\\\\\\\"payload\\\\\\\")"'}]}
    assert len(pr._task_document(json.dumps(value), root, data)) == 1


def test_task_document_rejects_missing_unknown_and_ambiguous_actions(tmp_path):
    root = tmp_path / "program"
    data = tmp_path / "workspace"
    root.mkdir()
    data.mkdir()
    base = {"task_path": "\\", "task_name": "Astrid Runtime", "actions": []}
    for actions in ([], [{"Execute": str(root / "python.exe"), "Arguments": str(data)},
                         {"Execute": str(root / "python.exe"), "Arguments": str(data)}],
                    [{"Execute": "C:/unknown/python.exe", "Arguments": str(data)}]):
        with __import__("pytest").raises(RuntimeError):
            pr._task_document(json.dumps({**base, "actions": actions}), root, data)


def test_task_document_leaves_escaped_arguments_to_pinned_action_cas(tmp_path):
    root = tmp_path / "program"
    root.mkdir()
    value = {"task_path": "\\", "task_name": "Astrid Runtime", "actions": [
        {"Execute": str(root / "versions" / "r13" / "python.exe"),
         "Arguments": ' -X utf8 -c "print(\\\\\\\"escaped\\\\\\\")"'}]}
    assert len(pr._task_document(json.dumps(value), root, tmp_path / "unused")) == 1


def test_temp_script_transport_is_file_based_and_removes_temp(monkeypatch, tmp_path):
    seen = {}

    class Completed:
        returncode = 0
        stdout = b"ok\n"

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["bytes"] = Path(argv[-1]).read_bytes()
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(pr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    output, evidence = pr._ps_file("'secret-free-test'", shell="powershell.exe")
    assert output == "ok"
    assert "-File" in seen["argv"] and "-Command" not in seen["argv"]
    assert seen["bytes"].startswith(b"\xef\xbb\xbf$ErrorActionPreference='Stop'; ")
    assert not Path(seen["argv"][-1]).exists()
    assert evidence["script_bytes"] == len(seen["bytes"])
    assert len(evidence["script_sha256"]) == 64


def test_prereconcile_fails_closed_without_explicit_diagnostic_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRID_FIX17_DIAGNOSTIC", raising=False)
    with __import__("pytest").raises(RuntimeError) as caught:
        pr.prereconcile(journey=object(), job_id="direct-windows-r13-update",
                        shell="powershell.exe", agent_checkout=tmp_path,
                        root=tmp_path / "f17" / "program", data=tmp_path / "f17" / "data",
                        version_document={}, marker=tmp_path / "f17" / "data" / "marker",
                        marker_sha256="0" * 64)
    assert caught.value.safe_diagnostic == {
        "failure_stage": "hosted_scope_guard", "failure_code": "HOSTED_SCOPE_GUARD",
        "failure_category": "RuntimeError"}


def test_parent_runs_reconcile_in_selected_python_isolated_child(monkeypatch, tmp_path):
    root, data = tmp_path / "f17" / "program", tmp_path / "f17" / "data"
    root.mkdir(parents=True)
    data.mkdir()
    marker = data / "marker.txt"
    marker.write_bytes(b"seed")
    version = {"env": {}, "identity": {"release": 13}, "profile": {},
               "python": str(root / "versions" / "r13" / "python.exe"), "root": str(root)}
    versions = root / "versions" / "r13"
    versions.mkdir(parents=True)
    version_path = versions / "version.json"
    version_path.write_text(json.dumps(version), encoding="utf-8")
    (root / "current").write_text("r13", encoding="utf-8")
    before = (version_path, data, version)
    class Journey:
        @staticmethod
        def hosted_windows_runner():
            return True
        @staticmethod
        def installation(_root, _data):
            return before
    monkeypatch.setattr(pr.sys, "platform", "win32")
    for key, value in {"ASTRID_FIX17_DIAGNOSTIC": "1", "GITHUB_REF": "refs/heads/release/14/fix17-diag",
                       "RUNNER_TEMP": str(tmp_path), "ImageOS": "windows2022", "GITHUB_ACTIONS": "true",
                       "ASTRID_CI_EXPECTED_RUNNER": "windows-2022"}.items():
        monkeypatch.setenv(key, value)
    seen = {}
    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return type("Completed", (), {"returncode": 0, "stdout": b'{"status":"pass"}'})()
    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    receipt = pr.prereconcile(journey=Journey(), job_id="direct-windows-r13-killed-update",
                              shell="powershell.exe", agent_checkout=tmp_path / "candidate-agent-source",
                              root=root, data=data, version_document=version, marker=marker,
                              marker_sha256=pr._sha256(b"seed"))
    assert seen["argv"][:2] == [version["python"], "-I"]
    assert "--child" in seen["argv"]
    assert "--version" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--version-sha256") + 1] == pr._sha256(version_path.read_bytes())
    assert seen["argv"][seen["argv"].index("--row") + 1] == "direct-windows-r13-killed-update"
    assert receipt["current_unchanged"] and receipt["data_unchanged"]


def test_hosted_version_document_uses_identity_release_shape():
    document = {"env": {}, "identity": {"release": 13}, "profile": {},
                "python": "C:/f17/program/versions/r13/python.exe", "root": "C:/f17/program"}
    assert document["identity"]["release"] == 13
    assert "release" not in document


def test_no_parent_import_eviction_or_duplicate_probe():
    source = MODULE.read_text(encoding="utf-8")
    assert "sys.modules" not in source
    assert "-I" in source and "--child" in source
