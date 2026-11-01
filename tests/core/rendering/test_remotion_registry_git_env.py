"""Registry validation from an ordinary agent/editor shell (which sets GIT_EDITOR)."""
import pytest

from astrid.core.rendering import remotion_runtime as runtime


def _env(tmp_path, **extra):
    return {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin', **extra}


def test_interactive_git_variables_are_accepted_and_never_reach_the_probe(tmp_path):
    context = runtime._registry_context(
        _env(tmp_path, GIT_EDITOR='true', GIT_PAGER='cat', GIT_TERMINAL_PROMPT='0'), generator=False)
    assert context['HOME'] == str(tmp_path)
    assert not any(key.startswith('GIT_') for key in context)


@pytest.mark.parametrize('name', ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_GLOBAL', 'GIT_INDEX_FILE', 'PYTHONSTARTUP'])
def test_ambient_git_redirection_and_startup_hooks_are_projected_out(tmp_path, name):
    clean = _env(tmp_path)
    ambient = {**clean, name: str(tmp_path / 'x')}
    for generator in (False, True):
        assert runtime._registry_context(ambient, generator=generator) == runtime._registry_context(clean, generator=generator)
    with pytest.raises(runtime.RemotionValidationError, match=name):
        runtime._validate_remotion_worker_environment(ambient)
