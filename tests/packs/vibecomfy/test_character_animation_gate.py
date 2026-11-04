from unittest.mock import Mock

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import availability
from astrid.packs.vibecomfy.actions.character_animation import run


def test_character_animation_gate_and_qualified_fake_dispatch(tmp_path, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(run.runtime, '_run', runner)
    argv = ['--capability', run.CAPABILITY_ID, '--out', str(tmp_path), '--task-identity', 'fake',
            '--profile', 'pip_embedded', '--reference-image-ref', 'image', '--driving-video-ref', 'video',
            '--mode', 'animate', '--resolution', '720p']
    with pytest.raises(AstridError) as exc:
        run.main(argv)
    assert exc.value.code == 'generation_route_unqualified'
    runner.assert_not_called()
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {'actions': {run.CAPABILITY_ID: {'available': True}}})
    assert run.main(argv) == 0
    runner.assert_called_once()
