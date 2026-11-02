from __future__ import annotations

from pathlib import Path

import pytest

from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import validate_pack


def _write_pack(root: Path, source: str) -> Path:
    pack = root / "demo"
    pack.mkdir()
    (pack / "entry.py").write_text(source, encoding="utf-8")
    return pack


def _manifest(**overrides):
    value = {
        "id": "demo", "schema_version": 3, "pack_api": "1.0",
        "dependencies": {"packs": [], "python_requirements": []},
    }
    value.update(overrides)
    return value


def _candidate(**overrides):
    value = {
        "id": "demo", "canonical_root": "/packs/demo", "schema_version": 3,
        "pack_api": "1.0", "dependencies": {"packs": []}, "commit": "abc",
        "installed_digest": "digest",
    }
    value.update(overrides)
    return value


def test_good_fixture_allows_stdlib_facade_own_and_declared_dependencies(tmp_path):
    pack = _write_pack(tmp_path, """\
import json
from astrid.pack_api import AstridError
from .helper import build
import astrid.packs.audio.api
import numpy
""")
    manifest = _manifest(dependencies={"packs": ["audio"], "python_requirements": ["numpy>=1.21"]})
    contract = ImportContract(facade_exports=frozenset({"AstridError"}),
                              core_pack_apis={"audio": "astrid.packs.audio.api"})
    assert check_pack_imports(pack, contract=contract, manifest=manifest) == []


def test_personal_pack_skips_only_static_import_scan(tmp_path):
    pack = _write_pack(tmp_path, "import astrid.core.internal\n")
    manifest = _manifest(id="personal")
    assert check_pack_imports(pack, contract=ImportContract(), manifest=manifest) == []


@pytest.mark.parametrize(("source", "requirements", "deps", "code", "line", "module"), [
    ("from astrid.core._shared.result_manifest import write_manifest\n", [], [], "deep_import", 1,
     "astrid.core._shared.result_manifest"),
    ("from astrid.pack_api import HiddenImplementation\n", [], [], "deep_import", 1,
     "astrid.pack_api.HiddenImplementation"),
    ("import astrid.packs.other.api\n", [], [], "undeclared_pack", 1, "astrid.packs.other.api"),
    ("import numpy\n", [], [], "undeclared_python", 1, "numpy"),
    ("importlib.import_module(name)\n", [], [], "dynamic_import", 1, ""),
    ("from importlib import import_module as load\nload('astrid.core.internal')\n", [], [], "deep_import", 2,
     "astrid.core.internal"),
    ("def broken(:\n", [], [], "syntax_error", 1, ""),
])
def test_import_violation_fixtures_report_file_and_line(tmp_path, source, requirements,
                                                         deps, code, line, module):
    pack = _write_pack(tmp_path, source)
    manifest = _manifest(dependencies={"packs": deps, "python_requirements": requirements})
    results = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [(item.path, item.line, item.module, item.code) for item in results] == [
        ("entry.py", line, module, code)
    ]


def _run(manifest=None, candidate=None, selected=None, pins=None, packages=None,
         violations=(), mode="inventory"):
    return validate_pack(
        mode=mode, manifest=manifest or _manifest(), candidate=candidate or _candidate(),
        selected=selected or {}, core_api=(1, 0), release_pins=pins or {},
        python_packages=packages or {}, import_violations=violations,
    )


@pytest.mark.parametrize(("manifest", "candidate", "selected", "pins", "packages", "violations", "mode", "expected"), [
    (_manifest(schema_version=4), None, None, None, None, (), "inventory", "unsupported_schema"),
    (_manifest(schema_version=2), None, None, None, None, (), "inventory", "schema_upgrade_required"),
    (_manifest(dependencies={"packs": "bad", "python_requirements": []}), None, None, None, None, (), "inventory", "invalid_manifest"),
    (None, None, {"demo": _candidate(id="other")}, None, None, (), "inventory", "duplicate_id"),
    (_manifest(id="core"), None, None, None, None, (), "inventory", "reserved_id"),
    (None, _candidate(canonical_root="/packs/mismatch"), None, None, None, (), "inventory", "basename_mismatch"),
    (_manifest(pack_api=None), None, None, None, None, (), "inventory", "api_missing"),
    (_manifest(pack_api="2.0"), None, None, None, None, (), "inventory", "api_major"),
    (_manifest(pack_api="1.1"), None, None, None, None, (), "inventory", "api_minor"),
    (_manifest(dependencies={"packs": ["missing"], "python_requirements": []}), None, None, None, None, (), "inventory", "missing_dep"),
    (_manifest(dependencies={"packs": ["audio"], "python_requirements": []}), None,
     {"audio": {"id": "audio", "dependencies": {"packs": ["demo"]}}}, None, None, (), "inventory", "cycle"),
    (_manifest(dependencies={"packs": ["audio"], "python_requirements": []}), None,
     {"audio": {"id": "audio", "commit": "wrong", "dependencies": {"packs": []}}},
     {"audio": {"commit": "pinned"}}, None, (), "inventory", "default_pin_conflict"),
    (None, _candidate(commit="pinned"), None, {"demo": {"commit": "pinned", "digest": "other"}}, None, (), "install", "pin_mismatch"),
    (_manifest(dependencies={"packs": [], "python_requirements": ["numpy>=1"]}), None, None, None, {}, (), "inventory", "missing_python"),
    (None, None, None, None, None, (object(),), "inventory", "import_violation"),
])
def test_validator_fail_closed_cases(manifest, candidate, selected, pins, packages,
                                     violations, mode, expected):
    result = _run(manifest, candidate, selected, pins, packages, violations, mode)
    assert result == {
        "version": 1, "pack_id": "core" if manifest and manifest.get("id") == "core" else "demo",
        "ok": False, "code": expected,
        "reason": result["reason"], "fix": result["fix"],
        "dependencies": result["dependencies"], "warnings": [],
    }
    assert result["reason"] and result["fix"]


def test_validator_success_is_deterministic_and_caret_compatible():
    manifest = _manifest(pack_api="1.0", dependencies={"packs": [], "python_requirements": []})
    first = _run(manifest, packages={})
    second = _run(manifest, packages={})
    assert first == second
    assert first == {"version": 1, "pack_id": "demo", "ok": True, "code": "compatible",
                     "reason": "", "fix": "", "dependencies": [], "warnings": []}
