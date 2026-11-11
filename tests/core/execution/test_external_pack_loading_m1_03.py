"""M1-03 real-host composition for selected external pack imports."""
from __future__ import annotations

import json
import os
import py_compile
import sys
from pathlib import Path

import yaml
import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.pack.identity import pack_digest
from tests.core.execution.test_generic_host_actions_v3 import FakeRuntime, pack as base_pack, task_for


CORE_MODULE = "astrid.pack_api"


@pytest.fixture(autouse=True)
def isolate_pack_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-source-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)


def _external_pack(tmp_path, monkeypatch, *, kind="python", module="astrid.packs.unrelated.actions.emit",
                   route="module", action_code=None, outputs=None):
    """Start with the canonical v3 fixture, then admit it through a fresh host."""
    root, _, runtime, _, _ = base_pack(tmp_path)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(root.parent))
    (root / "actions").mkdir(exist_ok=True)
    (root / "shared").mkdir(exist_ok=True)
    for package in (root / "actions", root / "shared"):
        (package / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "shared" / "helper.py").write_text("VALUE = 'same-value'\n", encoding="utf-8")
    manifest = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
    manifest["pack_api"] = "2.0"
    action = manifest["actions"]["calculate"]
    if kind == "python":
        invocation = {"kind": "python", "path": "actions/emit.py", "function": "run"}
    else:
        if route == "inline":
            inline = ("import json, sys; "
                      "from astrid.packs.unrelated.actions.emit import facts; "
                      "from pathlib import Path; "
                      "Path(sys.argv[1]).write_text(json.dumps(facts(), sort_keys=True), encoding='utf-8')")
            argv = ["{python_exec}", "-c", inline, "{out}/answer.json"]
        elif route == "script":
            launcher = root / "launch_script.py"
            launcher.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "from astrid.packs.unrelated.actions.emit import facts\n"
                "Path(sys.argv[1]).write_text(json.dumps(facts(), sort_keys=True), encoding='utf-8')\n",
                encoding="utf-8")
            argv = ["{python_exec}", str(launcher), "{out}/answer.json"]
        elif route == "package":
            (root / "__main__.py").write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "from astrid.packs.unrelated.actions.emit import facts\n"
                "Path(sys.argv[1]).write_text(json.dumps(facts(), sort_keys=True), encoding='utf-8')\n",
                encoding="utf-8")
            argv = ["{python_exec}", "-m", "astrid.packs.unrelated", "{out}/answer.json"]
        else:
            argv = ["{python_exec}", "-m", module, "{out}/answer.json"]
        invocation = {"kind": "command", "command": {"argv": argv}}
    action["invocation"] = invocation
    action["inputs"] = []
    action["outputs"] = outputs or []
    (root / "actions" / "emit.py").write_text(action_code or _python_action(), encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root, runtime, GenericPackHost(pack_roots=[root], client=runtime)


def _facts_source(core_module=CORE_MODULE):
    return (
        f"import {core_module} as core\n"
        "from ..shared import helper as relative_helper\n"
        "from astrid.packs.unrelated.shared import helper as absolute_helper\n"
        "import json, os, sys\n"
        "def facts():\n"
        "    return {'module': (__spec__.name if __spec__ is not None else __name__), 'file': __file__, 'value': absolute_helper.VALUE, "
        "'same_module': relative_helper is absolute_helper, 'core_name': core.__name__, "
        "'core_path': list(getattr(core, '__path__', [])), 'core_file': getattr(core, '__file__', None), "
        "'runtime_file': sys.modules['runtime_protocol'].__file__, "
        "'sys_path': sys.path, 'pythonpath': os.environ.get('PYTHONPATH', '')}\n"
    )


def _python_action():
    return _facts_source() + "def run():\n    return facts()\n"


def _command_action():
    return _facts_source() + (
        "if __name__ == '__main__':\n"
        "    from pathlib import Path\n"
        "    Path(sys.argv[1]).write_text(json.dumps(facts(), sort_keys=True), encoding='utf-8')\n"
    )


def _run(host, runtime, *, task_id="pack-task"):
    host.register()
    task = task_for(runtime)
    task["task"]["id"] = task_id
    task["task"]["attempt_id"] = f"{task_id}-attempt"
    runtime.tasks[task_id] = task
    runtime.tasks.pop("python-task", None)
    result = host.run_task(task, lease_token="lease")
    return result


def _assert_facts(facts, root):
    import runtime_protocol

    assert facts["module"] == "astrid.packs.unrelated.actions.emit"
    assert ".__external_" not in facts["module"]
    assert Path(facts["file"]).resolve() == (root / "actions" / "emit.py").resolve()
    assert facts["value"] == "same-value" and facts["same_module"] is True
    assert facts["core_name"] == CORE_MODULE
    core_file = (Path(__file__).resolve().parents[3] / "astrid" / "pack_api.py").resolve()
    assert Path(facts["core_file"]).resolve() == core_file
    assert str(root) not in facts["sys_path"] and str(root.parent) not in facts["sys_path"]
    assert str(root) not in facts["pythonpath"].split(os.pathsep)
    assert str(root.parent) not in facts["pythonpath"].split(os.pathsep)
    assert Path(facts["runtime_file"]).resolve() == Path(runtime_protocol.__file__).resolve()


def test_real_host_python_action_uses_canonical_selected_namespace(tmp_path, monkeypatch):
    root, runtime, host = _external_pack(tmp_path, monkeypatch)
    host.discover()
    result = _run(host, runtime)
    assert result["task"]["status"] == "completed"
    receipt = runtime.settlements[0][2]
    _assert_facts(receipt["result"]["action_result"], root)


def test_worker_uses_host_runtime_origin_without_ambient_pythonpath(tmp_path, monkeypatch):
    root, runtime, host = _external_pack(tmp_path, monkeypatch)
    ambient = tmp_path / "site-packages"
    shadow = ambient / "runtime_protocol"
    shadow.mkdir(parents=True)
    sentinel = tmp_path / "ambient-runtime-imported"
    (shadow / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(ambient))
    host.discover()
    assert _run(host, runtime)["task"]["status"] == "completed"
    facts = runtime.settlements[0][2]["result"]["action_result"]
    _assert_facts(facts, root)
    assert str(ambient) not in facts["sys_path"]
    assert str(ambient) not in facts["pythonpath"].split(os.pathsep)
    assert not sentinel.exists()


def test_raw_selected_loader_preserves_real_in_tree_pack_imports(tmp_path, monkeypatch):
    """Exercise the loader separately from the host's public import checker.

    Current foundations permit external actions to use the public core facade;
    deep core-pack imports are checked by their own compatibility owner.
    """
    import subprocess
    import runtime_protocol

    root, _, _ = _external_pack(tmp_path, monkeypatch,
        action_code=_facts_source("astrid.packs.generation"))
    sibling = root.parent / "ambient"
    (sibling / "shared").mkdir(parents=True)
    sentinel = tmp_path / "unselected-imported"
    (sibling / "shared" / "tripwire.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    script = """
import importlib, json, sys, runtime_protocol
from pathlib import Path
from astrid.core.pack.imports import install_pack_imports
root = Path(sys.argv[1])
prefix = install_pack_imports(root.name, root)
print(json.dumps(importlib.import_module(prefix + '.actions.emit').facts()))
try:
    importlib.import_module('astrid.packs.ambient.shared.tripwire')
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('unselected sibling imported')
"""
    repo_root = Path(__file__).resolve().parents[3]
    runtime_root = Path(runtime_protocol.__file__).resolve().parent.parent
    completed = subprocess.run([sys.executable, "-c", script, str(root)], cwd=tmp_path,
        env={"HOME": str(tmp_path / "loader-home"),
             "PYTHONPATH": os.pathsep.join((str(repo_root), str(runtime_root))),
             "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert not sentinel.exists()
    facts = json.loads(completed.stdout)
    assert facts["module"] == "astrid.packs.unrelated.actions.emit"
    assert Path(facts["file"]).resolve() == root / "actions" / "emit.py"
    assert facts["same_module"] is True and facts["value"] == "same-value"
    assert facts["core_name"] == "astrid.packs.generation"
    assert (repo_root / "astrid" / "packs" / "generation").resolve() in [
        Path(path).resolve() for path in facts["core_path"]]


@pytest.mark.parametrize("route", ["module", "inline", "script", "package"])
def test_real_host_python_command_routes_use_same_selected_namespace_and_settle_bytes(tmp_path, monkeypatch, route):
    outputs = [{"name": "answer", "type": "file", "mode": "create",
                "path_template": "{out}/answer.json", "artifact_type": "application/json"}]
    root, runtime, host = _external_pack(tmp_path, monkeypatch, kind="command", route=route,
        action_code=_command_action(), outputs=outputs)
    host.discover()
    result = _run(host, runtime, task_id="command-task")
    assert result["task"]["status"] == "completed"
    uploaded = next(iter(runtime.uploaded_objects.values()))
    _assert_facts(json.loads(uploaded["data"].decode("utf-8")), root)
    assert runtime.settlements[0][2]["outputs"]


def test_fresh_admission_observes_raw_edit_and_old_admission_refuses(tmp_path, monkeypatch):
    root, runtime, host = _external_pack(tmp_path, monkeypatch)
    host.discover()
    definition, old_admission = host.admit("executor", "unrelated.calculate")
    helper = root / "shared" / "helper.py"
    helper_stat = helper.stat()
    py_compile.compile(str(helper), doraise=True)
    helper.write_text("VALUE = 'diff-value'\n", encoding="utf-8")
    os.utime(helper, ns=(helper_stat.st_atime_ns, helper_stat.st_mtime_ns))
    pin = {"version": 1, "source": "https://example.invalid/packs#unrelated", "commit": "1" * 40,
           "digest_rule": "pack-tree-v1", "digest": "0" * 64, "origin": "user"}
    (root / "PIN").write_text(json.dumps(pin) + "\n", encoding="utf-8")

    with pytest.raises(HostError, match="source digest changed"):
        host.invoke_capability(capability_kind="executor", capability_id="unrelated.calculate",
            request={"inputs": {}, "out": str(tmp_path / "old-out"), "project": "demo",
                     "project_was_auto_resolved": True, "python_exec": sys.executable,
                     "invocation": "runtime"}, attempt=tmp_path / "old-attempt",
            definition=definition, admission=old_admission)

    new_definition, new_admission = host.admit("executor", "unrelated.calculate")
    assert new_admission["source_digest"] == pack_digest(root)
    assert new_admission["source_digest"] != old_admission["source_digest"]
    result = _run(host, runtime, task_id="edited-task")
    assert result["task"]["status"] == "completed"
    assert runtime.settlements[-1][2]["result"]["action_result"]["value"] == "diff-value"
    assert new_definition["id"] == definition["id"]


def test_unselected_visible_sibling_is_not_imported_by_selected_action(tmp_path, monkeypatch):
    root, runtime, host = _external_pack(tmp_path, monkeypatch)
    sibling = root.parent / "ambient"
    (sibling / "shared").mkdir(parents=True)
    sentinel = tmp_path / "ambient-imported"
    (sibling / "__init__.py").write_text("\n", encoding="utf-8")
    (sibling / "shared" / "__init__.py").write_text("\n", encoding="utf-8")
    (sibling / "shared" / "tripwire.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    action = root / "actions" / "emit.py"
    action.write_text(_facts_source() +
        "import astrid.packs.ambient.shared.tripwire\n"
        "def run():\n    return facts()\n", encoding="utf-8")
    host = GenericPackHost(pack_roots=[root], client=runtime)
    host.discover()
    with pytest.raises(HostError):
        _run(host, runtime, task_id="isolated-selection")
    assert not sentinel.exists()
    assert runtime.settlements == [] and runtime.uploaded_objects == {}


def test_unsupported_bare_module_command_refuses_before_child_execution(tmp_path, monkeypatch):
    root, runtime, _ = _external_pack(tmp_path, monkeypatch, kind="command", module="unrelated.actions.emit",
        action_code=("from pathlib import Path\n"
                     f"Path({str(tmp_path / 'bare-command-ran')!r}).touch()\n"))
    host = GenericPackHost(pack_roots=[root], client=runtime)
    host.discover()
    from astrid.core.execution import generic_host as generic_host_module
    def unexpected_child_acquisition(*_args, **_kwargs):
        pytest.fail("unsupported command route acquired a child")
    monkeypatch.setattr(generic_host_module, "spawn_task_scope", unexpected_child_acquisition)
    with pytest.raises(HostError):
        _run(host, runtime, task_id="unsupported-route")
    assert not (tmp_path / "bare-command-ran").exists()
    assert runtime.settlements == [] and runtime.uploaded_objects == {}


@pytest.mark.parametrize("pack_id, refusal", [
    # cbf7b424 deliberately made the canonical-personal-root guard run before
    # the in-tree collision check, so a personal pack outside that root is
    # refused with this exact message.
    ("personal", "selected personal pack must use the canonical personal root"),
    ("generation", "reserved|collides"),
])
def test_selected_import_reuses_reserved_and_core_namespace_policy(tmp_path, pack_id, refusal):
    from astrid.core.pack.imports import install_pack_imports

    root = tmp_path / pack_id
    root.mkdir()
    sentinel = tmp_path / "foreign-initializer"
    (root / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    with pytest.raises((ValueError, ModuleNotFoundError), match=refusal):
        install_pack_imports(pack_id, root)
    assert not sentinel.exists()


def test_selected_personal_import_uses_canonical_foundation_root(tmp_path):
    import subprocess

    home = tmp_path / "home"
    root = home / "Astrid" / "packs" / "personal"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("VALUE = 'canonical-personal'\n", encoding="utf-8")
    script = """
import importlib, sys
from pathlib import Path
from astrid.core.pack.imports import install_pack_imports
root = Path(sys.argv[1])
module = importlib.import_module(install_pack_imports('personal', root))
assert module.VALUE == 'canonical-personal'
assert Path(module.__file__).resolve() == root / '__init__.py'
"""
    repo_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run([sys.executable, "-c", script, str(root)], cwd=tmp_path,
        env={"HOME": str(home), "PYTHONPATH": str(repo_root), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


def test_changed_import_epoch_requires_fresh_process_without_module_eviction(tmp_path):
    import subprocess

    root = tmp_path / "epoch_fixture"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "helper.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    script = """
import importlib, sys
from pathlib import Path
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.imports import install_pack_imports
root = Path(sys.argv[1])
prefix = install_pack_imports(root.name, root, source_digest=pack_digest(root))
module = importlib.import_module(prefix + '.helper')
assert module.VALUE == 'old'
(root / 'helper.py').write_text("VALUE = 'new'\\n")
try:
    install_pack_imports(root.name, root, source_digest=pack_digest(root))
except ValueError as error:
    assert 'fresh action worker' in str(error)
else:
    raise AssertionError('changed epoch reused a live worker')
assert sys.modules[prefix + '.helper'] is module
assert module.VALUE == 'old'
print('epoch-refused-with-module-retained')
"""
    repo_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run([sys.executable, "-c", script, str(root)], cwd=tmp_path,
        env={"PYTHONPATH": str(repo_root), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "epoch-refused-with-module-retained"
