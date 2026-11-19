"""LS-12: one source read at admission, immutable Python bytes afterward."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from astrid.core.pack.identity import pack_digest


def _pack(tmp_path):
    root = tmp_path / "collection" / "ls12_fixture"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "helper.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (root / "resource.txt").write_text("included in the admitted content identity\n", encoding="utf-8")
    (root / "empty_namespace").mkdir()
    return root


def test_verified_capture_is_read_once_during_worker_admission(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import _portable_pack_capture, _selected_pack_generation

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    original = _pack(tmp_path)
    record = SimpleNamespace(definition=SimpleNamespace(metadata={
        "source_pack": original.name, "pack_root": str(original)}))
    with _portable_pack_capture(_selected_pack_generation(record)) as captured:
        root = captured[2]
        selected = {root.name: {"root": str(root), "source_digest": pack_digest(root)}}
        relocations = {root.name: {"original_root": str(original), **selected[root.name]}}
        script = """
import importlib, json, sys
from collections import Counter
from pathlib import Path
from astrid.core.pack.imports import install_pack_imports
selected, relocations = json.loads(sys.argv[1]), json.loads(sys.argv[2])
root = Path(next(iter(selected.values()))['root'])
reads = Counter()
read_bytes = Path.read_bytes
def count(path):
    if path.is_relative_to(root):
        reads[path.relative_to(root).as_posix()] += 1
    return read_bytes(path)
Path.read_bytes = count
prefix = install_pack_imports(root.name, root, source_digest=selected[root.name]['source_digest'],
                             selected_map=selected, verified_relocations=relocations)
assert importlib.import_module(prefix + '.helper').VALUE == 'old'
assert importlib.import_module(prefix + '.empty_namespace').__path__
print(json.dumps(dict(reads), sort_keys=True))
"""
        completed = subprocess.run([sys.executable, "-c", script, json.dumps(selected), json.dumps(relocations)],
                                   env=dict(os.environ), capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {"__init__.py": 1, "helper.py": 1, "resource.txt": 1}


def test_admitted_python_bytes_are_held_and_next_admission_detects_same_size_edit(tmp_path):
    root = _pack(tmp_path)
    script = """
import importlib, os, sys
from pathlib import Path
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.imports import install_pack_imports, validate_selected_pack_map
root = Path(sys.argv[1])
digest = pack_digest(root)
selected = {root.name: {'root': str(root), 'source_digest': digest}}
prefix = install_pack_imports(root.name, root, source_digest=digest, selected_map=selected)
helper = root / 'helper.py'
stamp = helper.stat()
helper.write_text("VALUE = 'new'\\n", encoding='utf-8')
os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
module = importlib.import_module(prefix + '.helper')
assert module.VALUE == 'old', 'an edit after admission changed executable bytes'
for check in (
    lambda: validate_selected_pack_map(selected),
    lambda: install_pack_imports(root.name, root, source_digest=digest, selected_map=selected),
):
    try: check()
    except ValueError as error: assert 'source digest changed' in str(error)
    else: raise AssertionError('fresh admission missed same-size restored-time edit')
try: install_pack_imports(root.name, root, source_digest=pack_digest(root))
except ValueError as error: assert 'fresh action worker' in str(error)
else: raise AssertionError('changed source reused the admitted worker')
assert sys.modules[prefix + '.helper'] is module and module.VALUE == 'old'
print('immutable-admission-and-fresh-edit-detection')
"""
    completed = subprocess.run([sys.executable, "-c", script, str(root)], env=dict(os.environ),
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "immutable-admission-and-fresh-edit-detection"
