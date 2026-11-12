"""CPU guard proof for the release H3 runtime declarations."""
import json
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.packs.h3_av.workflows.native_h3_continuation.workflow import READY_METADATA
from vibecomfy.runtime.dependencies import compare_runtime


@pytest.mark.parametrize('case', ['matching', 'stale_commit', 'wrong_version'])
def test_h3_managed_guard_enforces_release_runtime(case):
    runtime = dict(READY_METADATA['requirements']['runtime'])
    if case == 'stale_commit':
        runtime['comfy_commit'] = '423cbd20511becb1f16122ab518e92e58bfa03d9'
    workflow = SimpleNamespace(requirements=SimpleNamespace(runtime=runtime), metadata={})
    flags = runtime['launch_flags']
    runner = SimpleNamespace(
        launch_spec=lambda: {'argv': flags},
        helper=lambda *args, **kwargs: {'expected': {'requirements': [], 'wheels': {}, 'constraints': []}},
    )
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.root = '/synthetic'
    observed = {'comfy_commit': '6bfaacc67c2103481e5f0c84d75257cd0581d86a',
                'comfy_version': '0.43.2' if case == 'wrong_version' else '0.36.0',
                'packages': {'torch': '2.10.0+cu130', 'comfy-kitchen': '0.2.35', 'comfy-aimdo': '0.5.5'},
                'launch_flags': flags, 'managed': True}
    prep._native = lambda program, requirements, *args: SimpleNamespace(stdout=json.dumps(
        compare_runtime(json.loads(requirements), target=observed)))
    if case == 'matching':
        prep._guard_runtime(workflow, runner, post=True)
    else:
        with pytest.raises(IntegrityError, match='conflicts with the installed recipe' if case == 'stale_commit' else 'comfy_version'):
            prep._guard_runtime(workflow, runner, post=True)
