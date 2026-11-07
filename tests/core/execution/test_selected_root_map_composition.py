"""Selected source closure across the real host, command, and Python descendant."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _product_roots():
    import runtime_protocol

    return [str(Path(__file__).resolve().parents[3]),
            str(Path(runtime_protocol.__file__).resolve().parent.parent)]


def _isolated(tmp_path, script, *arguments):
    # Same explicit product roots and -I boundary as the attested-import test.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("ASTRID_PACKS_PATH", None)
    env.update(HOME=str(tmp_path / "home"), ASTRID_SOURCE_STATE=str(tmp_path / "absent.json"),
               ASTRID_HOST_EXECUTION_PROFILE="source", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-I", "-B", "-c",
        "import sys,json;sys.path[:0]=json.loads(sys.argv[1]);\n" + script,
        json.dumps(_product_roots()), *map(str, arguments)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _manifest(root, dependencies=(), actions=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "pack_api": "2.0", "id": root.name,
        "name": root.name, "version": "1.0.0", "dependencies": {"packs": list(dependencies)},
        "actions": actions or {},
    }))


def test_parent_command_python_grandchild_use_selected_map_and_exact_origins(tmp_path):
    roots = {name: tmp_path / ("collection_" + name) / name
             for name in ("map_parent", "map_dependency", "map_leaf")}
    decoys = [tmp_path / "ambient", tmp_path / "unselected"]
    for collection in decoys:
        for name in roots:
            decoy = collection / name
            decoy.mkdir(parents=True)
            (decoy / "__init__.py").write_text("raise AssertionError('ambient pack executed')\n")
            (decoy / "pack.yaml").write_text("unselected: never-read\n")
    unrelated = tmp_path / "unrelated_collection" / "map_unrelated"
    _manifest(unrelated)
    audit = '''
import os, sys
from pathlib import Path
def deny_unselected(event, args):
    if event in {'open', 'os.listdir', 'os.scandir'} and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).absolute()
        assert not any(path.is_relative_to(Path(root)) for root in FORBIDDEN), (event, str(path))
sys.addaudithook(deny_unselected)
'''.replace("FORBIDDEN", repr([str(path) for path in (*decoys, unrelated.parent)]))
    metadata = {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"}
    actions = {
        "parent": {"description": "Selected Python parent", "inputs": [], "outputs": {},
                   "invocation": {"kind": "python", "path": "actions/parent.py", "function": "run"},
                   "metadata": metadata},
        "command": {"description": "Selected Python command", "inputs": [], "outputs": [],
                    "invocation": {"kind": "command", "command": {"argv": [
                        "{python_exec}", "-m", "astrid.packs.map_parent.actions.command"]}},
                    "metadata": metadata},
    }
    _manifest(roots["map_parent"], ["map_dependency"], actions)
    _manifest(roots["map_dependency"], ["map_leaf"])
    _manifest(roots["map_leaf"])
    (roots["map_leaf"] / "api.py").write_text("def origins():\n    return {'map_leaf': __file__}\n")
    (roots["map_dependency"] / "api.py").write_text(
        "from astrid.packs.map_leaf.api import origins as leaf_origins\n"
        "def origins():\n    return {'map_dependency': __file__, **leaf_origins()}\n")
    (roots["map_parent"] / "api.py").write_text(audit + '''
import json
import astrid.pack_api as core
from astrid.packs.map_dependency.api import origins
def facts():
    return {'origins': {'map_parent': __file__, **origins()}, 'pid': os.getpid(),
            'map': json.loads(os.environ['ASTRID_SELECTED_PACK_MAP']),
            'packs_path': os.environ['ASTRID_PACKS_PATH'].split(os.pathsep),
            'pythonpath': os.environ.get('PYTHONPATH', '').split(os.pathsep),
            'sys_path': sys.path, 'core': core.__file__,
            'runtime': sys.modules['runtime_protocol'].__file__}
''')
    action_dir = roots["map_parent"] / "actions"
    action_dir.mkdir()
    (action_dir / "parent.py").write_text(
        "from astrid.packs.map_parent.api import facts\ndef run():\n    return facts()\n")
    grandchild = ("import sys,json;sys.path[:0]=json.loads(sys.argv[1]);\n" + audit
                  + "import runtime_protocol\nfrom astrid.packs.map_parent.api import facts\nprint(json.dumps(facts()))\n")
    (action_dir / "command.py").write_text('''
import json, os, subprocess, sys
from astrid.packs.map_parent.api import facts
_child_bridge = sys.modules['astrid.sdk._child_bridge']
assert _child_bridge._bridge is not None
assert not os.get_inheritable(_child_bridge._bridge._channel.fileno())
command = facts()
''' + f"child = subprocess.run([sys.executable, '-B', '-c', {grandchild!r}, {json.dumps(_product_roots())!r}], "
        "capture_output=True, text=True, timeout=15)\n"
        "assert child.returncode == 0, child.stderr\n"
        "print(json.dumps({'command': command, 'grandchild': json.loads(child.stdout)}))\n")
    script = '''
import importlib, os, socket, runtime_protocol
from pathlib import Path
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.pack.imports import install_pack_imports
roots = json.loads(sys.argv[2])
host = GenericPackHost(pack_roots=[*roots.values(), sys.argv[5]])
# Discovery may read the configured unrelated root (open M1-15b scope).
# After discovery, even map construction must leave that root unread.
host.discover()
''' + audit + '''
definition, admission = host.admit('executor', 'map_parent.parent')
selected = admission['selected_pack_map']
assert set(selected) == set(roots)
attempt = Path(sys.argv[3]) / 'parent-attempt'
result = host.invoke_capability(capability_kind='executor', capability_id='map_parent.parent',
    request={'inputs': {}, 'out': str(attempt / 'outputs'), 'project': 'demo',
             'project_was_auto_resolved': True, 'python_exec': sys.executable, 'invocation': 'runtime'},
    attempt=attempt, definition=definition, admission=admission)
python_parent = result.payload['action_result']
# Poison ambient inputs after discovery. Parent imports and both descendants
# must use the admission map instead of these same-ID external decoys.
os.environ['ASTRID_PACKS_PATH'] = sys.argv[4]
sys.path.insert(0, sys.argv[4])
install_pack_imports('map_parent', Path(roots['map_parent']),
    source_digest=selected['map_parent']['source_digest'], selected_map=selected)
parent = importlib.import_module('astrid.packs.map_parent.api').facts()
_, command_admission = host.admit('executor', 'map_parent.command')
attempt = Path(sys.argv[3]) / 'command-attempt'
(attempt / 'outputs').mkdir(parents=True)
# Same real socket descriptor pattern as the private-child-command test.
parent_socket, inherited = socket.socketpair()
class Bridge:
    def inherited_fd(self):
        return inherited.fileno()
    def launched(self):
        inherited.close()
try:
    command_result = host._run_command_definition(host.capabilities['map_parent.command'], {},
        attempt / 'outputs', attempt, admission=command_admission, child_bridge=Bridge())
    inherited.close()
    parent_socket.settimeout(2)
    assert parent_socket.recv(1) == b''
finally:
    inherited.close()
    parent_socket.close()
# A transitive dependency edit invalidates an old admission before any action
# executes, even though the selected parent's own source digest is unchanged.
leaf = Path(roots['map_leaf']) / 'api.py'
leaf.write_text(leaf.read_text() + '\\n# edited dependency\\n')
try:
    host.invoke_capability(capability_kind='executor', capability_id='map_parent.parent',
        request={'inputs': {}}, attempt=Path(sys.argv[3]) / 'stale-attempt',
        definition=definition, admission=admission)
except HostError as exc:
    assert 'source digest changed' in str(exc)
else:
    raise AssertionError('old transitive dependency admission executed')
_, fresh = host.admit('executor', 'map_parent.parent')
assert fresh['selected_pack_map']['map_leaf'] != selected['map_leaf']
assert fresh['source_digest'] == admission['source_digest']
assert command_result.returncode == 0
print(json.dumps({'parent': parent, 'python_parent': python_parent,
                  **json.loads((attempt / 'task-stdout-tail.log').read_text())}))
'''
    facts = _isolated(tmp_path, script, json.dumps({name: str(root) for name, root in roots.items()}),
                      tmp_path, decoys[0], unrelated)
    expected_origins = {name: str(root / "api.py") for name, root in roots.items()}
    expected_collections = {str(root.parent) for root in roots.values()}
    pids = set()
    for stage in ("parent", "python_parent", "command", "grandchild"):
        row = facts[stage]
        assert row["origins"] == expected_origins
        assert set(row["packs_path"]) == expected_collections
        assert row["map"] == facts["parent"]["map"]
        assert Path(row["core"]) == Path(_product_roots()[0]) / "astrid/pack_api.py"
        assert Path(row["runtime"]) == Path(_product_roots()[1]) / "runtime_protocol/__init__.py"
        assert not (expected_collections | {str(root) for root in roots.values()}) & set(row["pythonpath"])
        if stage != "parent":
            assert not expected_collections & set(row["sys_path"])
        pids.add(row["pid"])
    assert len(pids) == 4


@pytest.mark.parametrize("route", ["loader", "namespace", "host"])
def test_external_pack_sharing_bundled_id_is_refused(tmp_path, route):
    root = tmp_path / "external" / "generation"
    _manifest(root)
    script = '''
import os
from pathlib import Path
root, route = Path(sys.argv[2]), sys.argv[3]
if route == 'loader':
    from astrid.core.pack.imports import install_pack_imports
    try:
        install_pack_imports('generation', root)
    except ValueError as exc:
        assert 'collides with an in-tree pack' in str(exc)
    else:
        raise AssertionError('external bundled-ID pack admitted')
elif route == 'namespace':
    os.environ['ASTRID_PACKS_PATH'] = str(root.parent)
    try:
        import astrid.packs
    except ImportError as exc:
        assert 'collides' in str(exc) and 'generation' in str(exc)
    else:
        raise AssertionError('namespace accepted a bundled-ID collision')
else:
    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.core.pack import PackValidationError
    try:
        GenericPackHost(pack_roots=[root]).discover()
    except PackValidationError as exc:
        assert "duplicate pack id 'generation'" in str(exc)
    else:
        raise AssertionError('host accepted a bundled-ID collision')
print(json.dumps({'refused': route}))
'''
    assert _isolated(tmp_path, script, root, route) == {"refused": route}
