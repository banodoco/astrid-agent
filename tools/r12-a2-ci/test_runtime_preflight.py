import pathlib, sys, tempfile, types, unittest
from unittest.mock import patch
import runtime_preflight as preflight

class RuntimePreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        self.agent, self.runtime = root/'agent', root/'runtime'
        (self.agent/'tests').mkdir(parents=True); (self.runtime/'tests').mkdir(parents=True)
        (self.agent/'tests/__init__.py').write_text('')
        for name in ('test_verified_host_launch','test_remote_worker_activation'):
            (self.runtime/'tests'/f'{name}.py').write_text('PROOF = True\n')
        self.protocol = types.ModuleType('runtime_protocol'); self.protocol.__file__ = str(self.runtime/'runtime_protocol/__init__.py')
        self.modules = patch.dict(sys.modules, {'runtime_protocol':self.protocol}); self.modules.start()
        for name in list(sys.modules):
            if name == 'tests' or name.startswith('tests.'): del sys.modules[name]
    def tearDown(self): self.modules.stop(); self.tmp.cleanup()
    def test_loads_agent_package_and_both_exact_runtime_fixtures(self):
        preflight.verify_runtime_fixtures(self.agent,self.runtime)
        self.assertEqual(pathlib.Path(sys.modules['tests'].__file__),self.agent/'tests/__init__.py')
        for name in ('test_verified_host_launch','test_remote_worker_activation'):
            self.assertEqual(pathlib.Path(sys.modules['tests.'+name].__file__),self.runtime/'tests'/f'{name}.py')
    def test_rejects_fixture_shadow_in_agent(self):
        (self.agent/'tests/test_verified_host_launch.py').write_text('')
        with self.assertRaisesRegex(RuntimeError,'fixture mismatch'): preflight.verify_runtime_fixtures(self.agent,self.runtime)
    def test_rejects_wrong_runtime_protocol(self):
        self.protocol.__file__=str(self.agent/'runtime_protocol/__init__.py')
        with self.assertRaisesRegex(RuntimeError,'checkout mismatch'): preflight.verify_runtime_fixtures(self.agent,self.runtime)
    def test_rejects_missing_fixture(self):
        (self.runtime/'tests/test_remote_worker_activation.py').unlink()
        with self.assertRaises(ModuleNotFoundError): preflight.verify_runtime_fixtures(self.agent,self.runtime)
    def test_rejects_preimported_tests_modules(self):
        sys.modules['tests'] = types.ModuleType('tests')
        with self.assertRaisesRegex(RuntimeError, 'already imported'): preflight.verify_runtime_fixtures(self.agent,self.runtime)
    def test_rejects_missing_agent_tests_package(self):
        (self.agent/'tests/__init__.py').unlink()
        with self.assertRaises(FileNotFoundError): preflight.verify_runtime_fixtures(self.agent,self.runtime)
if __name__=='__main__': unittest.main()
