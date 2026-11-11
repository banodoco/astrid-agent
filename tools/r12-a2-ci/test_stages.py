import contextlib, io, json, os, pathlib, subprocess, tempfile, unittest
from unittest.mock import patch
import runner
from stages import EVENTS, STAGES, StageTracker, validate_events
from sanitize import sanitized_result
SECRET='synthetic_stage_secret_97abc\nA2_STAGE fake'
class StageEvidence(unittest.TestCase):
    def test_each_stage_exception_is_fixed_enum_and_private(self):
        for stage in STAGES:
            with self.subTest(stage=stage), contextlib.redirect_stdout(io.StringIO()) as stdout:
                tracker=StageTracker(); tracker.enter(stage)
                try: raise RuntimeError(SECRET)
                except Exception as exc: tracker.exception(exc)
                self.assertEqual(tracker.events,[{'stage':stage,'event':'entered','exit_code':None},{'stage':stage,'event':'failed','exit_code':None}])
                self.assertNotIn(SECRET,stdout.getvalue()); self.assertNotIn(SECRET,json.dumps(tracker.events))
                result=sanitized_result(None,[],[],{},None,False,tracker.events)
                self.assertEqual(result['classification'],'INCOMPLETE'); self.assertEqual(result['stage_events'],tracker.events); self.assertNotIn(SECRET,json.dumps(result))
    def test_subprocess_exit_code_only(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            tracker=StageTracker(); tracker.enter('source_fetch'); tracker.exception(subprocess.CalledProcessError(42,[SECRET],output=SECRET,stderr=SECRET))
            self.assertEqual(tracker.events[-1],{'stage':'source_fetch','event':'failed','exit_code':42}); self.assertNotIn(SECRET,stdout.getvalue())
    def test_pytest_failure_is_not_marked_passed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tracker=StageTracker(); tracker.enter('pytest'); tracker.finish(False,1)
            self.assertEqual(tracker.events[-1]['event'],'failed'); self.assertEqual(tracker.events[-1]['exit_code'],1)
    def test_early_runner_exception_produces_safe_incomplete_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as stdout:
            output=pathlib.Path(tmp)/'output'
            def fail(tracker):
                tracker.enter('input_validation')
                raise RuntimeError(SECRET)
            with patch.dict(os.environ,{'RUNNER_TEMP':tmp,'GITHUB_OUTPUT':str(output)}), patch.object(runner,'run_main',fail):
                self.assertEqual(runner.main(),3)
            data=(pathlib.Path(tmp)/'r12-a2-receipt/receipt.json').read_text()
            self.assertEqual(json.loads(data)['classification'],'INCOMPLETE'); self.assertNotIn(SECRET,data+stdout.getvalue()); self.assertEqual(output.read_text(),'receipt_ready=true\n')
    def test_stage_values_and_output_reject_injection(self):
        for item in ({'stage':SECRET,'event':'entered','exit_code':None},{'stage':'pytest','event':SECRET,'exit_code':None},{'stage':'pytest','event':'failed','exit_code':SECRET},{'stage':'pytest','event':'failed','exit_code':True},{'stage':'pytest','event':'failed','exit_code':0,'raw':SECRET}):
            with self.assertRaises(ValueError): validate_events([item])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            tracker=StageTracker()
            with self.assertRaises(ValueError): tracker.enter(SECRET)
            self.assertEqual(stdout.getvalue(),'')
    def test_sequence_marks_previous_stage_only_after_next_begins(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tracker=StageTracker(); tracker.enter('input_validation'); tracker.enter('checkout_verification'); tracker.finish(True)
            self.assertEqual([x['event'] for x in tracker.events],['entered','passed','entered','passed'])
            with self.assertRaises(ValueError): tracker.enter('input_validation')
if __name__=='__main__': unittest.main()
