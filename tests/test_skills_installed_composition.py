"""Installed core/profile/skill composition proof.

The in-repo installed-wheel precedent is
``tests/reshape/test_timeline_installed_skills.py::test_timeline_skill_lifecycle_on_installed_wheel``;
this test reuses its ``scripts.reshape.installed_artifact.build_once`` boundary
and its installed-copy identity checks. It intentionally does not use the
precedent's ambient Runtime ``.pth`` injection.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from scripts.reshape.installed_artifact import (
    _require_installed_matches_wheel,
    build_once,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = ROOT.parents[1]
RUNTIME_COMMIT = "1e6d899efa77a2d6ec247c38250f9748330bb587"
RUNTIME_RELATIVE = Path("sources/runtime-1e6d899")
OPTIONAL_PACK = "pixel"
OPTIONAL_CAPABILITY = "pixel.sprites"


def _run_json(harness, lane: str, script: str) -> dict:
    record = harness.run_lane(lane, ["-c", script], check=False)
    assert record.passed, record.output
    return json.loads(record.stdout)


def _parse_json(record, *, lane: str) -> dict:
    assert record.returncode == 0, f"{lane} failed:\n{record.output}"
    return json.loads(record.stdout)


def _filesystem_snapshot(root: Path) -> dict[str, tuple[str, str]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, str]] = {}
    for path in sorted((root, *root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", path.readlink().as_posix())
        elif path.is_file():
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[relative] = ("directory", "")
    return result


def _locked_requirement_blocks(lock_path: Path, names: set[str]) -> str:
    """Return the hashed lock stanzas needed by Runtime's declared dependencies."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        begins_requirement = bool(line) and not line[0].isspace() and not line.startswith("#")
        if begins_requirement and current:
            blocks.append(current)
            current = []
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    selected = []
    for block in blocks:
        match = re.match(r"([A-Za-z0-9_.-]+)==", block[0])
        if match and match.group(1).lower().replace("_", "-") in names:
            selected.extend(block)
    found = {
        match.group(1).lower().replace("_", "-")
        for block in blocks
        if (match := re.match(r"([A-Za-z0-9_.-]+)==", block[0]))
        and match.group(1).lower().replace("_", "-") in names
    }
    assert found == names, f"Runtime dependency lock subset missing {sorted(names - found)}"
    return "\n".join(selected) + "\n"


def _record_evidence(wheel: Path, site_packages: Path) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        record_name = next(
            name for name in archive.namelist()
            if name.endswith(".dist-info/RECORD") and "/" in name
        )
        expected = list(csv.reader(archive.read(record_name).decode("utf-8").splitlines()))
    installed_record = site_packages / record_name
    assert installed_record.is_file(), f"installed RECORD missing: {installed_record}"
    installed = list(csv.reader(installed_record.read_text(encoding="utf-8").splitlines()))
    installed_by_path = {row[0]: row[1:] for row in installed}
    hashed_rows = [row for row in expected if len(row) > 1 and row[1]]
    assert hashed_rows
    assert all(installed_by_path.get(row[0]) == row[1:] for row in hashed_rows)
    for row in hashed_rows:
        algorithm, encoded = row[1].split("=", 1)
        assert algorithm == "sha256"
        actual = base64.urlsafe_b64encode(hashlib.sha256((site_packages / row[0]).read_bytes()).digest()).rstrip(b"=").decode()
        assert actual == encoded, row[0]
    return {
        "record_member": record_name,
        "installed_record": str(installed_record),
        "hashed_file_count": len(hashed_rows),
        "identity": hashlib.sha256(installed_record.read_bytes()).hexdigest(),
    }


def _installed_view_probe_script() -> str:
    return r'''
import importlib.resources
import json
import sys
from pathlib import Path
import astrid
from astrid.skills import _sync_descriptors_for_harness, default_pack_ids, state
from astrid.skills.discovery import capability_index, list_skills
from astrid.mcp_server import build_prompts, build_tools, _extra_tools, handle

package = importlib.resources.files("astrid")
core_root = package.joinpath("core", "resources", "skills")
core_files = (
    "SKILL.md",
    "creative-work/SKILL.md",
    "creative-work/references/packs.md",
    "pack-builder/SKILL.md",
    "pack-builder/references/writing-skills.md",
    "pack-testing/SKILL.md",
    "references/capabilities.md",
)
core_text = {name: core_root.joinpath(*name.split("/")).read_text(encoding="utf-8") for name in core_files}
profile = package.joinpath("agent_profiles", "astrid.md").read_text(encoding="utf-8")
mains = list_skills()
current = state.load()
selected = _sync_descriptors_for_harness(mains, current, "claude", deep=False)
defaults = set(default_pack_ids(mains))
optional_ids = sorted({d.pack_id for d in selected} - {"_core"})
index = capability_index(selected)
prompts = build_prompts()
mcp_state = {"tools": {**build_tools(), **_extra_tools()}, "prompts": prompts}
listed = handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}, mcp_state)
return_value = {
    "astrid_file": str(Path(astrid.__file__).resolve()),
    "python": sys.executable,
    "sys_path": sys.path,
    "core_path": str(core_root),
    "core_resource_files": sorted(core_text),
    "core_resource_markers": {name: text.splitlines()[0] for name, text in core_text.items()},
    "core_descriptor_path": str(next(d.skill_md for d in mains if d.pack_id == "_core")),
    "profile_has_new_core_path": "{{ASTRID_PACKAGE_ROOT}}/core/resources/skills/SKILL.md" in profile,
    "profile_has_retired_core_path": "packs/_core" in profile,
    "resolved_profile": __import__("astrid.omp_agent", fromlist=["ASTRID_SYSTEM_PROMPT"]).ASTRID_SYSTEM_PROMPT,
    "prompt_args": __import__("astrid.omp_agent", fromlist=["_prompt_args"])._prompt_args([], env={"ASTRID_HOST_EXECUTION_PROFILE": "installed"}),
    "tool_names": sorted(mcp_state["tools"]),
    "selected_ids": sorted(d.pack_id for d in selected),
    "optional_ids": optional_ids,
    "capability_index": index,
    "prompt_names": sorted(prompts),
    "prompt_text": "\n".join(p["text"] for p in prompts.values()),
    "mcp_prompt_names": sorted(row["name"] for row in listed["result"]["prompts"]),
}
print(json.dumps(return_value))
'''


def _refusal_probe_script() -> str:
    return r'''
import hashlib
import json
import os
import subprocess
from pathlib import Path
import astrid.mcp_server as server

def snapshot(root):
    root = Path(root)
    if not root.exists():
        return {}
    rows = {}
    for path in sorted((root, *root.rglob("*"))):
        rel = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            rows[rel] = ["symlink", path.readlink().as_posix()]
        elif path.is_file():
            rows[rel] = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
        else:
            rows[rel] = ["directory", ""]
    return rows

roots = [os.environ["ASTRID_HOME"], os.environ["HOME"]]
before = {root: snapshot(root) for root in roots}
calls = []
def forbidden_process(*args, **kwargs):
    calls.append(repr(args[:1]))
    raise AssertionError("missing capability crossed the MCP process boundary")
server._run = forbidden_process
subprocess.run = forbidden_process
subprocess.Popen = forbidden_process
tool = server._extra_tools()["invoke"]
reply = server.call_tool(tool, {
    "capability_id": "pixel.sprites",
    "kind": "executor",
    "project": "installed-composition-refusal-probe",
    "inputs": {},
})
after = {root: snapshot(root) for root in roots}
print(json.dumps({"reply": reply, "process_calls": calls, "before": before, "after": after}))
'''


def test_installed_core_help_profile_and_selected_empty_sync(tmp_path: Path) -> None:
    workspace = tmp_path / "installed-skills-composition"
    runtime_root = (TASK_ROOT / RUNTIME_RELATIVE).resolve()
    assert runtime_root.is_dir(), runtime_root
    assert Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]).resolve() == runtime_root
    pythonpath = [Path(item).resolve() for item in os.environ["PYTHONPATH"].split(os.pathsep) if item]
    assert runtime_root in pythonpath, f"pinned Runtime checkout absent from PYTHONPATH: {pythonpath}"
    runtime_head = subprocess.run(
        ["git", "-C", str(runtime_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert runtime_head == RUNTIME_COMMIT

    harness = build_once(ROOT, workspace=workspace, install_dependencies=True)
    try:
        # Runtime declares jsonschema/referencing; select their complete pinned
        # transitive subset from Astrid's hashed runtime lock in the private venv.
        lock_path = ROOT / "requirements/runtime.lock"
        runtime_dependency_names = {
            "attrs", "jsonschema", "jsonschema-specifications", "referencing", "rpds-py",
        }
        dependency_subset = workspace / "runtime-dependency-subset.lock"
        dependency_subset.write_text(
            _locked_requirement_blocks(lock_path, runtime_dependency_names), encoding="utf-8"
        )
        private_env = harness.environment({"ASTRID_RUNTIME_CHECKOUT": str(runtime_root)})
        install_subset = subprocess.run(
            [str(harness.python_executable), "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", "--no-deps", "--require-hashes", "-r", str(dependency_subset)],
            cwd=workspace / "outside-checkout",
            env=private_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert install_subset.returncode == 0, install_subset.stdout + install_subset.stderr

        runtime_dist = workspace / "runtime-dist"
        runtime_dist.mkdir()
        build_python = harness.workspace / "build-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        build_env = dict(private_env)
        build_runtime = subprocess.run(
            [str(build_python), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(runtime_dist)],
            cwd=runtime_root,
            env=build_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert build_runtime.returncode == 0, build_runtime.stdout + build_runtime.stderr
        runtime_wheels = sorted(runtime_dist.glob("*.whl"))
        assert len(runtime_wheels) == 1, runtime_wheels
        runtime_wheel = runtime_wheels[0]
        runtime_digest = sha256_file(runtime_wheel)
        install_runtime = subprocess.run(
            [str(harness.python_executable), "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", "--no-deps", str(runtime_wheel)],
            cwd=workspace / "outside-checkout",
            env=private_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert install_runtime.returncode == 0, install_runtime.stdout + install_runtime.stderr
        pip_check = subprocess.run(
            [str(harness.python_executable), "-m", "pip", "check"],
            cwd=workspace / "outside-checkout",
            env=private_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert pip_check.returncode == 0, pip_check.stdout + pip_check.stderr

        identity = harness.probe_identity()
        assert identity.wheel_sha256 == harness.artifact.sha256
        agent_site = Path(identity.import_path).parent.parent
        agent_record = _record_evidence(harness.artifact.path, agent_site)
        with zipfile.ZipFile(runtime_wheel) as archive:
            runtime_record_member = next(
                name for name in archive.namelist()
                if name.endswith(".dist-info/RECORD") and "/" in name
            )
        runtime_record_path = agent_site / runtime_record_member
        runtime_package_file = agent_site / "runtime_protocol" / "__init__.py"
        assert runtime_package_file.is_file()
        _require_installed_matches_wheel(runtime_wheel, runtime_package_file)
        runtime_record = _record_evidence(runtime_wheel, agent_site)

        # Preserve the installed identity first, then exercise actual absent-pack
        # composition. Staging keeps the original wheel files available for the
        # one-pack and deselection checks without injecting a source checkout.
        packs_root = Path(identity.import_path).parent / "packs"
        staged_packs = workspace / "staged-optional-packs"
        staged_packs.mkdir()
        for directory in sorted(packs_root.iterdir()):
            if directory.is_dir() and any((directory / name).is_file() for name in ("pack.yaml", "pack.yml", "pack.json")):
                shutil.move(str(directory), staged_packs / directory.name)
        assert (staged_packs / OPTIONAL_PACK / "pack.yaml").is_file()
        harness.roots.home.joinpath(".claude").mkdir(parents=True, exist_ok=True)
        help_record = harness.run_console("installed-astrid-help", ["--help"], check=True)
        harness.run_module("installed-module-help", "astrid", ["--help"], check=True)
        harness.run_module("installed-skills-help", "astrid.skills", ["--help"], check=True)
        harness.run_module("installed-profile-resource", "astrid.omp_agent", ["--agent-profile-file"], check=True)
        assert "_core" not in help_record.stdout

        first_sync = harness.run_console(
            "installed-skills-empty-sync", ["skills", "sync", "--harness", "claude", "--json"], check=True
        )
        _parse_json(first_sync, lane="empty selected sync")
        zero = _run_json(harness, "installed-view-zero-optional", _installed_view_probe_script())
        assert not (Path(identity.import_path).parent / "packs" / "_core").exists()
        assert len(zero["core_resource_files"]) == 7
        assert zero["profile_has_new_core_path"] is True
        assert zero["profile_has_retired_core_path"] is False
        assert Path(zero["core_descriptor_path"]).is_file()
        assert zero["optional_ids"] == []
        assert zero["selected_ids"] == ["_core"]
        assert str(Path(identity.import_path).parent / "core/resources/skills/SKILL.md") in zero["resolved_profile"]
        assert str(Path(identity.import_path).parent / "core/resources/skills/SKILL.md") in zero["prompt_args"][1]
        assert OPTIONAL_CAPABILITY not in zero["capability_index"]
        assert "pixel" not in {name.lower() for name in zero["prompt_names"]}
        assert "pixel" not in {name.lower() for name in zero["mcp_prompt_names"]}

        shutil.move(str(staged_packs / OPTIONAL_PACK), packs_root / OPTIONAL_PACK)
        import yaml
        selected_manifest = yaml.safe_load((packs_root / OPTIONAL_PACK / "pack.yaml").read_text())
        assert selected_manifest["id"] == OPTIONAL_PACK
        assert OPTIONAL_CAPABILITY.split(".", 1)[1] in selected_manifest["actions"]
        install_pixel = harness.run_console(
            "installed-skills-select-pixel",
            ["skills", "install", OPTIONAL_PACK, "--harness", "claude", "--json"],
            check=True,
        )
        _parse_json(install_pixel, lane="selected optional pack install")
        one = _run_json(harness, "installed-view-one-optional", _installed_view_probe_script())
        assert one["optional_ids"] == [OPTIONAL_PACK]
        assert one["selected_ids"] == ["_core", OPTIONAL_PACK]
        assert OPTIONAL_CAPABILITY in one["capability_index"]
        assert "pixel" in {name.lower() for name in one["prompt_names"]}
        assert "pixel" in {name.lower() for name in one["mcp_prompt_names"]}
        assert OPTIONAL_CAPABILITY in one["prompt_text"]

        uninstall_pixel = harness.run_console(
            "installed-skills-deselect-pixel",
            ["skills", "uninstall", OPTIONAL_PACK, "--harness", "claude", "--json"],
            check=True,
        )
        _parse_json(uninstall_pixel, lane="optional pack deselection")
        final_sync = harness.run_console(
            "installed-skills-sync-after-deselection",
            ["skills", "sync", "--harness", "claude", "--json"], check=True,
        )
        _parse_json(final_sync, lane="sync after deselection")
        deselected = _run_json(harness, "installed-view-after-deselection", _installed_view_probe_script())
        assert OPTIONAL_PACK not in deselected["optional_ids"]
        assert OPTIONAL_CAPABILITY not in deselected["capability_index"]
        assert "pixel" not in {name.lower() for name in deselected["prompt_names"]}

        # After deselection, make the capability physically absent as well.
        shutil.move(str(packs_root / OPTIONAL_PACK), staged_packs / OPTIONAL_PACK)
        refusal = _run_json(harness, "installed-missing-capability-refusal", _refusal_probe_script())
        assert refusal["reply"].get("isError") is True, refusal["reply"]
        assert refusal["process_calls"] == [], refusal["process_calls"]
        assert refusal["before"] == refusal["after"], "missing capability changed state or home files"
        refusal_text = json.dumps(refusal["reply"]).lower()
        assert any(word in refusal_text for word in ("unavailable", "not selected", "missing", "unknown")), refusal

        runtime_probe = _run_json(
            harness,
            "installed-runtime-provenance",
            "import json, runtime_protocol, sys; print(json.dumps({'runtime_protocol_file': runtime_protocol.__file__, 'sys_path': sys.path}))",
        )
        runtime_installed_path = Path(runtime_probe["runtime_protocol_file"]).resolve()
        assert runtime_installed_path.is_relative_to(Path(harness.venv_dir).resolve())
        assert not runtime_installed_path.is_relative_to(runtime_root)

        evidence_path = workspace / "installed-skills-composition-evidence.json"
        evidence = {
            "test": "test_installed_core_help_profile_and_selected_empty_sync",
            "provenance": {
                "runtime_checkout": str(runtime_root),
                "runtime_commit": runtime_head,
                "astrid_runtime_checkout_env": os.environ["ASTRID_RUNTIME_CHECKOUT"],
                "pythonpath": os.environ["PYTHONPATH"],
                "test_interpreter": sys.executable,
                "installed_interpreter": identity.executable,
                "installed_astrid_file": zero["astrid_file"],
                "installed_sys_path": zero["sys_path"],
                "installed_runtime_protocol_file": str(runtime_installed_path),
                "runtime_sys_path": runtime_probe["sys_path"],
            },
            "astrid_wheel": {
                **harness.artifact.as_dict(),
                "record": agent_record,
            },
            "runtime_wheel": {
                "path": str(runtime_wheel),
                "sha256": runtime_digest,
                "record_member": runtime_record_member,
                "record_path": str(runtime_record_path),
                "record": runtime_record,
            },
            "runtime_dependency_subset": sorted(runtime_dependency_names),
            "runtime_dependency_lock": str(lock_path),
            "zero_optional": zero,
            "one_selected_optional": one,
            "deselected": deselected,
            "missing_capability_refusal": refusal,
            "commands": {
                "help": list(help_record.command),
                "runtime_dependency_install": [str(harness.python_executable), "-m", "pip", "install", "--require-hashes", "-r", str(dependency_subset)],
                "runtime_wheel_build": [str(build_python), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(runtime_dist)],
                "runtime_wheel_install": [str(harness.python_executable), "-m", "pip", "install", "--no-deps", str(runtime_wheel)],
            },
        }
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"installed composition evidence: {evidence_path}")
        print(f"Astrid wheel SHA256: {harness.artifact.sha256}")
        print(f"Runtime wheel SHA256: {runtime_digest}")
        print(f"installed astrid.__file__: {zero['astrid_file']}")
        print(f"installed interpreter: {identity.executable}")
        print(f"installed sys.path: {json.dumps(zero['sys_path'])}")
        print(f"Runtime provenance: {runtime_root}@{runtime_head}")
    finally:
        harness.close()
