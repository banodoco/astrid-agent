import contextlib, io, json, os, pathlib, tempfile, types, unittest
from unittest.mock import patch
from full_plan import RANGES, SIDES, group_for
from full_driver import decide
import full_driver
from runner import MANIFEST_SHA, validate, verify_run_control
from full_plan import FULL_REF
from sanitize import sanitized_result

class FullPlanTests(unittest.TestCase):
    def test_matrix_exactly_covers_131_new_side_groups(self):
        pairs=[]
        for side in SIDES:
            for name in RANGES:
                for slot in range(1,13):
                    group=group_for(side,name,slot)
                    if group: pairs.append((side,group))
        self.assertEqual(len(pairs),131); self.assertEqual(len(set(pairs)),131)
        self.assertEqual(set(pairs),{(s,f'{n:03}') for s in SIDES for n in range(20,53)} - {('r10','020')})
    def test_invalid_matrix_inputs_fail_closed(self):
        for args in [('r10','020-030',0),('r10','020-030',13),('../r10','020-030',1),('r10','018-030',1),('base','031-041',True)]:
            with self.assertRaises(ValueError): group_for(*args)
        self.assertIsNone(group_for('base','031-041',12)); self.assertIsNone(group_for('base','042-052',12))
    def test_continue_only_complete_results_or_explicit_absence(self):
        meta={'side':'base','group':'020','manifest_sha256':MANIFEST_SHA}
        complete={'classification':'COMPLETE','exit_code':1,'metadata':meta,'preflight_complete':True}
        self.assertTrue(decide(1,complete,True,'base','020'))
        self.assertFalse(decide(1,complete,False,'base','020'))
        self.assertFalse(decide(3,complete,True,'base','020'))
        self.assertFalse(decide(1,complete,True,'candidate','020'))
        self.assertFalse(decide(1,{**complete,'classification':'INCOMPLETE'},True,'base','020'))
        noresults={'classification':'NO_RESULTS','exit_code':0,'expected_count':0,'absent_ids':['known-id'],'metadata':meta,'preflight_complete':True}
        self.assertTrue(decide(0,noresults,True,'base','020'))
        self.assertFalse(decide(0,{**noresults,'absent_ids':[]},True,'base','020'))
        self.assertFalse(decide(0,{**noresults,'preflight_complete':False},True,'base','020'))
    def test_run_control_rejects_cross_range_or_replay(self):
        event={'A2_MODE':'full','A2_SIDE':'base','A2_REF':FULL_REF,'A2_RANGE':'020-030','A2_BEFORE':'0'*40,'A2_ATTEMPT':'1'}
        verify_run_control('020',event)
        for group,override in [('031',{}),('019',{}),('020',{'A2_SIDE':'r10'}),('020',{'A2_REF':'refs/heads/main'}),('020',{'A2_ATTEMPT':'2'}),('020',{'A2_BEFORE':'a'*40}),('020',{'A2_RANGE':'anything'}),('020',{'A2_MODE':'anything'})]:
            with self.subTest(group=group,override=override), self.assertRaises(ValueError): verify_run_control(group,{**event,**override})
    def test_zero_results_require_verified_absence(self):
        self.assertEqual(sanitized_result(None,[],['known'],{},0,True)['classification'],'NO_RESULTS')
        for result in (sanitized_result(None,[],[],{},0,True),sanitized_result(None,[],['known'],{},0,False),sanitized_result(b'',[],['known'],{},0,True)):
            self.assertEqual(result['classification'],'INCOMPLETE')
    def test_frozen_inventory_all_pairs(self):
        for side in SIDES:
            for groups in RANGES.values():
                for group in groups:
                    batch,ids=validate(side,group); self.assertEqual(len(ids),batch['counts'][side])
    def test_driver_strips_runtime_secrets_and_marks_accounted_failure(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            env={'RUNNER_TEMP':tmp,'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_REF':FULL_REF,'A2_SHA':'a'*40,'A2_BEFORE':'0'*40,'A2_ATTEMPT':'1','ACTIONS_RUNTIME_TOKEN':'synthetic-token','GITHUB_OUTPUT':'/synthetic-output'}
            data={'classification':'COMPLETE','exit_code':1,'preflight_complete':True,'metadata':{'side':'base','group':'020','manifest_sha256':MANIFEST_SHA}}
            def child(command,env,start_new_session):
                self.assertTrue(start_new_session); self.assertEqual(env['A2_GROUP'],'020'); self.assertEqual(env['A2_MODE'],'full')
                self.assertNotIn('ACTIONS_RUNTIME_TOKEN',env); self.assertNotIn('GITHUB_OUTPUT',env)
                return types.SimpleNamespace(returncode=1)
            with patch.dict(os.environ,env), patch.object(full_driver,'verify_prior_receipts',return_value=True), patch.object(full_driver,'census',side_effect=[{(1,1)},{(1,1)}]), patch.object(full_driver.subprocess,'run',side_effect=child), patch.object(full_driver,'rebuild',return_value=data):
                self.assertEqual(full_driver.main(),0)
            self.assertTrue((pathlib.Path(tmp)/'r12-a2-full-had-failure').is_file())
            receipt=json.loads((pathlib.Path(tmp)/'r12-a2-final-slot-01/receipt.json').read_text())
            self.assertEqual(receipt['classification'],'COMPLETE'); self.assertEqual(receipt['exit_code'],1); self.assertEqual(receipt['process_census']['status'],'clear')
    def test_residual_process_stops_without_uploadable_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            env={'RUNNER_TEMP':tmp,'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_REF':FULL_REF,'A2_SHA':'a'*40,'A2_BEFORE':'0'*40,'A2_ATTEMPT':'1'}
            with patch.dict(os.environ,env), patch.object(full_driver,'verify_prior_receipts',return_value=True), patch.object(full_driver,'census',side_effect=[{(1,1)},{(1,1),(2,3)}]), patch.object(full_driver.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), patch.object(full_driver,'rebuild') as rebuild:
                self.assertEqual(full_driver.main(),3); rebuild.assert_not_called()
            self.assertFalse((pathlib.Path(tmp)/'r12-a2-final-slot-01/receipt.json').exists())
    def test_unverified_census_stops_without_uploadable_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            env={'RUNNER_TEMP':tmp,'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_REF':FULL_REF,'A2_SHA':'a'*40,'A2_BEFORE':'0'*40,'A2_ATTEMPT':'1'}
            with patch.dict(os.environ,env), patch.object(full_driver,'verify_prior_receipts',return_value=True), patch.object(full_driver,'census',side_effect=[{(1,1)},RuntimeError('synthetic')]), patch.object(full_driver.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), patch.object(full_driver,'rebuild') as rebuild:
                self.assertEqual(full_driver.main(),3); rebuild.assert_not_called()
            self.assertFalse((pathlib.Path(tmp)/'r12-a2-final-slot-01/receipt.json').exists())
    def test_precreated_slot_path_fails_and_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            env={'RUNNER_TEMP':tmp,'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_REF':FULL_REF,'A2_SHA':'a'*40,'A2_BEFORE':'0'*40,'A2_ATTEMPT':'1'}
            slot=pathlib.Path(tmp)/'r12-a2-final-slot-01'; slot.mkdir(); (slot/'receipt.json').write_text('synthetic-secret')
            data={'classification':'COMPLETE','exit_code':0,'preflight_complete':True,'metadata':{'side':'base','group':'020','manifest_sha256':MANIFEST_SHA}}
            with patch.dict(os.environ,env), patch.object(full_driver,'verify_prior_receipts',return_value=True), patch.object(full_driver,'census',side_effect=[{(1,1)},{(1,1)}]), patch.object(full_driver.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), patch.object(full_driver,'rebuild',return_value=data):
                self.assertEqual(full_driver.main(),3)
            called=(pathlib.Path(__file__).resolve().parents[2]/'.github/workflows/r12-a2-full-range.yml').read_text()
            self.assertIn("steps.slot01.outcome == 'success'",called)
    def test_workflow_bounds_and_per_slot_checkpoint(self):
        root=pathlib.Path(__file__).resolve().parents[2]/'.github/workflows'
        caller=(root/'r12-a2-full.yml').read_text(); called=(root/'r12-a2-full-range.yml').read_text()
        self.assertEqual(caller.count('uses: ./.github/workflows/r12-a2-full-range.yml'),12)
        for side in SIDES:
            for number in (1,2):
                prev=f'{side}_range{number}'
                self.assertIn(f'needs: {prev}',caller)
                self.assertIn(f"needs.{prev}.outputs.safe_to_continue == 'true'",caller)
                self.assertIn(f'needs.{prev}.outputs.has_failure',caller)
        self.assertIn('timeout-minutes: 360',called)
        self.assertEqual(called.count('python tools/r12-a2-ci/full_driver.py'),11)
        self.assertEqual(called.count('exec env -i PATH='),11)
        for line in called.splitlines():
            if 'exec env -i PATH=' in line:
                self.assertIn('RUNNER_TEMP="$RUNNER_TEMP"',line)
                self.assertNotIn('GITHUB_OUTPUT',line)
                self.assertNotIn('ACTIONS_RUNTIME_TOKEN',line)
        self.assertNotIn('outputs.artifact_path',called)
        for slot in range(1,12): self.assertIn('path: ${{ runner.temp }}/r12-a2-final-slot-%02d/receipt.json' % slot, called)
        self.assertEqual(called.count('if-no-files-found: error'),11)
        for slot in range(1,12): self.assertIn("steps.slot%02d.outcome == 'success'" % slot,called)
        for slot in range(1,12): self.assertIn("A2_SLOT: '%s'" % slot, called)
        self.assertEqual(called.count('A2_SLOT:'),22)
        self.assertIn('r12-a2-full-had-failure',called)
        self.assertIn('safe_to_continue=true',called)
        self.assertNotIn('workflow_dispatch:',caller)
if __name__ == '__main__': unittest.main()
