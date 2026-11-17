import hashlib
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_receipt_upload", ROOT / "scripts" / "check_receipt_upload.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def build(tmp_path, files):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    lines = [f"{hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()}  {n}\n" for n in sorted(files)]
    (tmp_path / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    return tmp_path


def test_hidden_file_fails_unless_upload_includes_hidden_files(tmp_path):
    build(tmp_path, {"a.log": "x", "artifacts/.astrid-registry-state.json": "{}"})
    assert guard.check(tmp_path, include_hidden_files=True) == []
    problems = guard.check(tmp_path, include_hidden_files=False)
    assert len(problems) == 1 and ".astrid-registry-state.json" in problems[0]
    assert guard.main([str(tmp_path)]) == 1
    assert guard.main([str(tmp_path), "--include-hidden-files"]) == 0


def test_hidden_directory_component_counts(tmp_path):
    build(tmp_path, {".state/x.json": "{}"})
    assert guard.check(tmp_path, include_hidden_files=False)
    assert guard.check(tmp_path, include_hidden_files=True) == []


def test_missing_listed_file_and_changed_bytes_fail(tmp_path):
    build(tmp_path, {"a.log": "x", "b.log": "y"})
    (tmp_path / "b.log").unlink()
    (tmp_path / "a.log").write_text("changed", encoding="utf-8")
    problems = guard.check(tmp_path, include_hidden_files=True)
    assert any("b.log" in p and "not in the directory" in p for p in problems)
    assert any("a.log" in p and "sha256" in p for p in problems)


def test_missing_manifest_fails(tmp_path):
    assert guard.check(tmp_path, include_hidden_files=True)


def test_lanes_upload_includes_hidden_files_and_guard_runs_before_it():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lanes.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["installed"]["steps"]
    names = [step.get("name") for step in steps]
    upload = steps[names.index("Upload installed-wheel receipt")]
    assert upload["with"]["include-hidden-files"] is True
    guard_step = steps[names.index("Check the receipt manifest against what will be uploaded")]
    assert names.index(guard_step["name"]) < names.index(upload["name"])
    assert "check_receipt_upload.py" in guard_step["run"] and "--include-hidden-files" in guard_step["run"]
