import importlib.util, pathlib, tempfile, unittest
from unittest.mock import patch
P=pathlib.Path(__file__).resolve().parents[2]/'scripts/mac_update_outer.py'
spec=importlib.util.spec_from_file_location('mac_outer',P);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Result:
 def __init__(self,code,out='',err=''):self.returncode=code;self.stdout=out;self.stderr=err
class Guards(unittest.TestCase):
 def test_absent_requires_exact_launchctl_error(self):
  msg='Could not find service "haus.astrid.runtime" in domain for user gui: 503'
  with patch.object(m.subprocess,'run',return_value=Result(113,err=msg)):self.assertTrue(m.service_absent())
  with patch.object(m.subprocess,'run',return_value=Result(113,err='Bad request.\n'+msg)):self.assertTrue(m.service_absent())
  with patch.object(m.subprocess,'run',return_value=Result(113,err='unexpected '+msg)):self.assertFalse(m.service_absent())
  with patch.object(m.subprocess,'run',return_value=Result(0,err=msg)):self.assertFalse(m.service_absent())
 def test_port_fail_closed(self):
  for r,want in ((Result(1),True),(Result(0,'listener'),False),(Result(1,err='permission denied'),False),(Result(2),False)):
   with patch.object(m.subprocess,'run',return_value=r):self.assertEqual(m.port_free(49152),want)
 def test_redaction(self):
  raw=b'/Users/example/private abc ghp_123456 https://x/';out=m.redact(raw,pathlib.Path('/Users/example'),pathlib.Path('/Users/example/private'))
  self.assertNotIn(b'/Users/example',out);self.assertNotIn(b'ghp_123456',out)
 def test_updater_census_is_runner_only(self):
  active=Result(0,'42 /bin/sh /task/app/public/install --update\n')
  with patch.object(m.subprocess,'run',return_value=active) as call:
   self.assertTrue(m.runner_updaters_exist());self.assertEqual(call.call_args.args[0][2:4],['-u','503'])
  with patch.object(m.subprocess,'run',return_value=Result(0,'44 python harmless\n')):self.assertFalse(m.runner_updaters_exist())
 def test_owner_detection(self):
  self.assertTrue(m.owner_match('python -m runtime_protocol start'))
  self.assertFalse(m.owner_match('python unrelated'))
if __name__=='__main__':unittest.main()
