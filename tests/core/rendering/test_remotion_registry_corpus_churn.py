"""Only changes Python could see may invalidate a launch-bound registry corpus."""
import pytest

from astrid.core.execution import generic_host
from astrid.core.rendering import remotion_runtime as runtime


def _observation(tree_root, startup_dir):
    return {'module_files': [], 'roots': [str(tree_root)], 'package_roots': [], 'optional_files': [],
            'search_paths': [str(startup_dir)]}


@pytest.fixture
def dirs(tmp_path):
    tree, startup = tmp_path / 'tree', tmp_path / 'startup'
    tree.mkdir()
    startup.mkdir()
    (tree / 'pack.yaml').write_text('name: a\n')
    (startup / 'existing.py').write_text('x = 1\n')
    return tree, startup


def _inputs(dirs):
    return runtime._registry_inputs(_observation(*dirs))


@pytest.mark.parametrize('name', ['.DS_Store', 'notes.txt', 'app.log', '.module.py.swp'])
def test_non_importable_startup_files_do_not_change_corpus(dirs, name):
    before = _inputs(dirs)
    (dirs[1] / name).write_text('x')
    (dirs[1] / '__pycache__').mkdir()
    assert _inputs(dirs) == before


def test_new_module_or_package_in_startup_dir_changes_corpus(dirs):
    before = _inputs(dirs)
    (dirs[1] / 'foo.py').write_text('y = 2\n')
    with_module = _inputs(dirs)
    assert with_module != before
    assert 'foo.py' in runtime._describe_corpus_change(before, with_module)
    (dirs[1] / 'pkg').mkdir()
    assert _inputs(dirs) != with_module


def test_tree_pack_yaml_edit_changes_corpus_but_generated_names_do_not(dirs):
    tree = dirs[0]
    before = _inputs(dirs)
    (tree / '.DS_Store').write_bytes(b'finder')
    (tree / '__pycache__').mkdir()
    (tree / '__pycache__' / 'm.cpython-311.pyc').write_bytes(b'pyc')
    assert _inputs(dirs) == before
    (tree / 'pack.yaml').write_text('name: b\n')
    assert _inputs(dirs) != before


def test_wrapper_message_includes_underlying_reason():
    exc = runtime.RemotionValidationError(
        'launch-bound registry corpus changed: a new module or file `foo.py` appeared in /x')
    message = generic_host._remotion_readiness_message(exc)
    assert message.startswith('worker Remotion readiness context is unavailable: ')
    assert '`foo.py` appeared in /x' in message
    assert message.endswith('restart Astrid to re-bind it')
    other = generic_host._remotion_readiness_message(runtime.RemotionValidationError('digest differs'))
    assert other.endswith('digest differs')
