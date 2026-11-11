import importlib.util, json, pathlib, tempfile, unittest
from unittest.mock import patch
p=pathlib.Path(__file__).with_name('runner.py'); spec=importlib.util.spec_from_file_location('a2_runner',p); r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
class RunnerValidation(unittest.TestCase):
    def test_all_136_inputs_match_frozen_inventory(self):
        for side in r.PINS:
            for group in range(19,53):
                batch, ids=r.validate(side,f'{group:03}')
                self.assertEqual(len(ids),batch['counts'][side])
    def test_reject_arbitrary_scope(self):
        for side,group in [('r10','018'),('r10','053'),('../x','019'),('base','19'),('base','019/../../x')]:
            with self.assertRaises(ValueError): r.validate(side,group)
    def test_tampered_manifest_and_control_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp); (p/'inventory').mkdir(); (p/'inventory/final-batches.json').write_text('[]')
            with patch.object(r,'HERE',p), self.assertRaisesRegex(ValueError,'manifest digest'): r.validate('base','019')
            (p/'inventory/final-batches.json').write_bytes((r.HERE/'inventory/final-batches.json').read_bytes())
            (p/'run-control.json').write_text('{}')
            with patch.object(r,'HERE',p), self.assertRaisesRegex(ValueError,'control mismatch'): r.validate('base','019')
    def test_zero_inventory_is_explicit(self):
        for group in ('021','022','024','025'): self.assertEqual(r.validate('r10',group)[1],[])
if __name__=='__main__': unittest.main()
